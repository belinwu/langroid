import hashlib
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    case,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.expression import insert

from langroid.embedding_models.base import EmbeddingFunction, EmbeddingModelsConfig
from langroid.embedding_models.models import OpenAIEmbeddingsConfig
from langroid.mytypes import DocMetaData, Document
from langroid.vector_store.base import VectorStore, VectorStoreConfig

logger = logging.getLogger(__name__)


class PostgresDBConfig(VectorStoreConfig):
    collection_name: str = "embeddings"
    cloud: bool = False
    docker: bool = True
    host: str = "127.0.0.1"
    port: int = 5432
    replace_collection: bool = False
    embedding: EmbeddingModelsConfig = OpenAIEmbeddingsConfig()
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200


class PostgresDB(VectorStore):
    def __init__(self, config: PostgresDBConfig = PostgresDBConfig()):
        super().__init__(config)
        self.config: PostgresDBConfig = config
        self.embedding_fn: EmbeddingFunction = self.embedding_model.embedding_fn()
        self.embedding_dim = self.embedding_model.embedding_dims
        self.engine = self._create_engine()
        PostgresDB._create_vector_extension(self.engine)
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )
        self.metadata = MetaData()
        self._setup_table()

    def _create_engine(self) -> Engine:
        if self.config.docker:
            username = os.getenv("POSTGRES_USER", "postgres")
            password = os.getenv("POSTGRES_PASSWORD", "postgres")
            database = os.getenv("POSTGRES_DB", "langroid")
            if not (
                self.config.host
                and self.config.port
                and username
                and password
                and database
            ):
                raise ValueError(
                    "Provide POSTGRES_USER, POSTGRES_PASSWORD and POSTGERS_DB."
                )
            connection_string = (
                f"postgresql+psycopg2://{username}:{password}@"
                f"{self.config.host}:{self.config.port}/{database}"
            )
            self.config.cloud = False
        elif self.config.cloud:
            connection_string = os.getenv("POSTGRES_CONNECTION_STRING")
            if not connection_string:
                raise ValueError(
                    "Provide the POSTGRES_CONNECTION_STRING for cloud config."
                )
        else:
            logger.warning(
                "Provide docker config or cloud config to connect to your database"
            )
        return create_engine(connection_string, pool_size=10, max_overflow=20)

    def _setup_table(self):
        try:
            from pgvector.sqlalchemy import Vector
        except ImportError:
            raise ImportError(
                "pgvector is not installed. Install with `pip install pgvector`."
            )

        if self.config.replace_collection:
            self.delete_collection(self.config.collection_name)

        self.embeddings_table = Table(
            self.config.collection_name,
            self.metadata,
            Column("id", String, primary_key=True, nullable=False, unique=True),
            Column("embedding", Vector(self.embedding_dim)),
            Column("document", String),
            Column("cmetadata", JSONB),
            extend_existing=True,
        )

        self.metadata.create_all(self.engine)
        self.metadata.reflect(bind=self.engine, only=[self.config.collection_name])

        # Create HNSW index if it doesn't exist
        index_name = f"hnsw_index_{self.config.collection_name}_embedding"
        with self.engine.connect() as connection:
            if not self.index_exists(connection, index_name):
                connection.execute(text("COMMIT"))
                create_index_query = text(
                    f"""
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}
                    ON {self.config.collection_name}
                    USING hnsw (embedding vector_cosine_ops)
                    WITH (
                        m = {self.config.hnsw_m},
                        ef_construction = {self.config.hnsw_ef_construction}
                    );
                    """
                )
                connection.execute(create_index_query)

    def index_exists(self, connection, index_name):
        """Check if an index exists."""
        query = text(
            "SELECT 1 FROM pg_indexes WHERE indexname = :index_name"
        ).bindparams(index_name=index_name)
        result = connection.execute(query).scalar()
        return bool(result)

    @staticmethod
    def _create_vector_extension(conn: Engine) -> None:
        with conn.connect() as connection:
            with connection.begin():
                statement = text(
                    "SELECT pg_advisory_xact_lock(1573678846307946496);"
                    "CREATE EXTENSION IF NOT EXISTS vector;"
                )
                connection.execute(statement)

    def set_collection(self, collection_name: str, replace: bool = False) -> None:
        inspector = inspect(self.engine)
        table_exists = collection_name in inspector.get_table_names()

        if (
            collection_name == self.config.collection_name
            and table_exists
            and not replace
        ):
            return
        else:
            self.config.collection_name = collection_name
            self.config.replace_collection = replace
            self._setup_table()

    def list_collections(self, empty: bool = True) -> List[str]:
        inspector = inspect(self.engine)
        table_names = inspector.get_table_names()

        with self.SessionLocal() as session:
            collections = []
            for table_name in table_names:
                table = Table(table_name, self.metadata, autoload_with=self.engine)
                if empty:
                    collections.append(table_name)
                else:
                    # Efficiently check for non-emptiness
                    if session.query(table.select().limit(1).exists()).scalar():
                        collections.append(table_name)
            return collections

    def create_collection(self, collection_name: str, replace: bool = False) -> None:
        self.set_collection(collection_name, replace=replace)

    def delete_collection(self, collection_name: str) -> None:
        """
        Deletes a collection and its associated HNSW index, handling metadata
        synchronization issues.
        """
        with self.engine.connect() as connection:
            connection.execute(text("COMMIT"))
            index_name = f"hnsw_index_{collection_name}_embedding"
            drop_index_query = text(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
            connection.execute(drop_index_query)

            # 3. Now, drop the table using SQLAlchemy
            table = Table(collection_name, self.metadata)
            table.drop(self.engine, checkfirst=True)

            # 4. Refresh metadata again after dropping the table
            self.metadata.clear()
            self.metadata.reflect(bind=self.engine)

    def clear_all_collections(self, really: bool = False, prefix: str = "") -> int:
        if not really:
            logger.warning("Not deleting all tables, set really=True to confirm")
            return 0

        inspector = inspect(self.engine)
        table_names = inspector.get_table_names()

        with self.SessionLocal() as session:
            deleted_count = 0
            for table_name in table_names:
                if table_name.startswith(prefix):
                    # Use delete_collection to handle index and table deletion
                    self.delete_collection(table_name)
                    deleted_count += 1
            session.commit()
            logger.warning(f"Deleted {deleted_count} tables with prefix '{prefix}'.")
            return deleted_count

    def clear_empty_collections(self) -> int:
        inspector = inspect(self.engine)
        table_names = inspector.get_table_names()

        with self.SessionLocal() as session:
            deleted_count = 0
            for table_name in table_names:
                table = Table(table_name, self.metadata, autoload_with=self.engine)

                # Efficiently check for emptiness without fetching all rows
                if session.query(table.select().limit(1).exists()).scalar():
                    continue

                # Use delete_collection to handle index and table deletion
                self.delete_collection(table_name)
                deleted_count += 1

            session.commit()  # Commit is likely not needed here
            logger.warning(f"Deleted {deleted_count} empty tables.")
            return deleted_count

    def _parse_embedding_store_record(self, res: Any) -> Dict[str, Any]:
        metadata = res.cmetadata or {}
        metadata["id"] = res.id
        return {
            "content": res.document,
            "metadata": DocMetaData(**metadata),
        }

    def get_all_documents(self, where: str = "") -> List[Document]:
        with self.SessionLocal() as session:
            query = session.query(self.embeddings_table)

            # Apply 'where' clause if provided
            if where:
                try:
                    where_json = json.loads(where)
                    query = query.filter(
                        self.embeddings_table.c.cmetadata.contains(where_json)
                    )
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON in 'where' clause: {where}")
                    return []  # Return empty list or handle error as appropriate

            results = query.all()
            documents = [
                Document(**self._parse_embedding_store_record(res)) for res in results
            ]
            return documents

    def get_documents_by_ids(self, ids: List[str]) -> List[Document]:
        with self.SessionLocal() as session:
            # Add a CASE statement to preserve the order of IDs
            case_stmt = case(
                {id_: index for index, id_ in enumerate(ids)},
                value=self.embeddings_table.c.id,
            )

            query = (
                session.query(self.embeddings_table)
                .filter(self.embeddings_table.c.id.in_(ids))
                .order_by(case_stmt)  # Order by the CASE statement
            )
            results = query.all()

            documents = [
                Document(**self._parse_embedding_store_record(row)) for row in results
            ]
            return documents

    def add_documents(self, documents: Sequence[Document]) -> None:
        super().maybe_add_ids(documents)
        for doc in documents:
            doc.metadata.id = str(PostgresDB._id_to_uuid(doc.metadata.id, doc.metadata))
        with self.SessionLocal() as session:
            new_records = []
            for doc in documents:
                metadatas = doc.dict().pop("metadata")
                record = {
                    "id": doc.metadata.id,
                    "embedding": self.embedding_fn([doc.content])[0],
                    "document": doc.content,
                    "cmetadata": metadatas,
                }
                new_records.append(record)

            if new_records:
                session.execute(insert(self.embeddings_table), new_records)
                session.commit()

    @staticmethod
    def _id_to_uuid(id: str, obj: object) -> str:
        try:
            doc_id = str(uuid.UUID(id))
        except ValueError:
            obj_repr = repr(obj)

            # Create a hash of the object representation
            obj_hash = hashlib.sha256(obj_repr.encode()).hexdigest()

            # Combine the ID and the hash
            combined = f"{id}-{obj_hash}"

            # Generate a UUID from the combined string
            doc_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, combined))

        return doc_id

    def similar_texts_with_scores(
        self,
        query: str,
        k: int = 1,
        where: Optional[str] = None,
        neighbors: int = 1,  # Parameter not used in this implementation
    ) -> List[Tuple[Document, float]]:
        embedding = self.embedding_fn([query])[0]

        with self.SessionLocal() as session:
            # Select only necessary columns, excluding 'embedding'
            if where is not None:
                try:
                    json_query = json.loads(where)
                except json.JSONDecodeError:
                    raise ValueError(f"Invalid JSON in 'where' clause: {where}")

                results = (
                    session.query(
                        self.embeddings_table.c.id,
                        self.embeddings_table.c.document,
                        self.embeddings_table.c.cmetadata,
                        (
                            1
                            - (
                                self.embeddings_table.c.embedding.cosine_distance(
                                    embedding
                                )
                            )
                        ).label("score"),
                    )
                    .filter(self.embeddings_table.c.cmetadata.contains(json_query))
                    .order_by(
                        self.embeddings_table.c.embedding.cosine_distance(embedding)
                    )
                    .limit(k)
                    .all()
                )
            else:
                results = (
                    session.query(
                        self.embeddings_table.c.id,
                        self.embeddings_table.c.document,
                        self.embeddings_table.c.cmetadata,
                        (
                            1
                            - (
                                self.embeddings_table.c.embedding.cosine_distance(
                                    embedding
                                )
                            )
                        ).label("score"),
                    )
                    .order_by(
                        self.embeddings_table.c.embedding.cosine_distance(embedding)
                    )
                    .limit(k)
                    .all()
                )

            documents_with_scores = []
            for result in results:
                id, document, cmetadata, score = result
                doc = Document(
                    content=document,
                    metadata=DocMetaData(**(cmetadata or {})),
                )
                documents_with_scores.append((doc, score))

            return documents_with_scores

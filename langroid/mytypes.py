from enum import Enum
from textwrap import dedent
from typing import Any, Callable, Dict, List, Union
from uuid import uuid4

from langroid.pydantic_v1 import BaseModel, Extra, Field, validator

Number = Union[int, float]
Embedding = List[Number]
Embeddings = List[Embedding]
EmbeddingFunction = Callable[[List[str]], Embeddings]


class Entity(str, Enum):
    """
    Enum for the different types of entities that can respond to the current message.
    """

    AGENT = "Agent"
    LLM = "LLM"
    USER = "User"
    SYSTEM = "System"

    def __eq__(self, other: object) -> bool:
        """Allow case-insensitive equality (==) comparison with strings."""
        if other is None:
            return False
        if isinstance(other, str):
            return self.value.lower() == other.lower()
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        """Allow case-insensitive non-equality (!=) comparison with strings."""
        return not self.__eq__(other)

    def __hash__(self) -> int:
        """Override this to ensure hashability of the enum,
        so it can be used sets and dictionary keys.
        """
        return hash(self.value.lower())


class DocMetaData(BaseModel):
    """Metadata for a document."""

    source: str = "context"  # just reference
    source_content: str = "context"  # reference and content
    title: str = "Unknown Title"
    published_date: str = "Unknown Date"
    is_chunk: bool = False  # if it is a chunk, don't split
    id: str = Field(default_factory=lambda: str(uuid4()))
    window_ids: List[str] = []  # for RAG: ids of chunks around this one

    @validator("source", "source_content", "id", "title", "published_date")
    def ensure_not_empty(cls, v: str) -> str:
        """Ensure required string fields are not empty."""
        if not v:
            raise ValueError("Field cannot be empty")
        return v

    def dict_bool_int(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """
        Special dict method to convert bool fields to int, to appease some
        downstream libraries,  e.g. Chroma which complains about bool fields in
        metadata.
        """
        original_dict = super().dict(*args, **kwargs)

        for key, value in original_dict.items():
            if isinstance(value, bool):
                original_dict[key] = 1 * value

        return original_dict

    class Config:
        extra = Extra.allow


class Document(BaseModel):
    """Interface for interacting with a document."""

    content: str
    metadata: DocMetaData

    def id(self) -> str:
        return self.metadata.id

    @staticmethod
    def from_string(
        content: str,
        source: str = "context",
        is_chunk: bool = True,
    ) -> "Document":
        return Document(
            content=content,
            metadata=DocMetaData(source=source, is_chunk=is_chunk),
        )

    def __str__(self) -> str:
        return dedent(
            f"""
        CONTENT: {self.content}         
        SOURCE:{self.metadata.source}
        """
        )


class NonToolAction(str, Enum):
    """
    Possible options to handle non-tool msgs from LLM.
    """

    FORWARD_USER = "user"  # forward msg to user
    DONE = "done"  # task done

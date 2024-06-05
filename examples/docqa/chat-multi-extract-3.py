"""
Variant of chat_multi_extract.py more suited to local LLM, using 3 Agents
(instead of 2 agents):

- LeaseExtractorAgent: is tasked with extracting structured information from a
    commercial lease document, and must present the terms in a specific nested JSON
    format. This agent generates questions corresponding to each field in the JSON
    format.
- Validator: This agent detects if LeaseExtractorAgent's message is asking for ONE
    piece of information, or MULTIPLE pieces. If the message is only asking about ONE
    thing, OR if it is NOT EVEN a question, it responds with "DONE" and says nothing.
    If the message is asking MORE THAN ONE thing, it responds with a message asking to
    only ask ONE question at a time.
    [Why restrict to one question at a time? Because the DocAgent is more likely to
      understand and answer a single question at a time]

- DocAgent: This agent answers the questions generated by LeaseExtractorAgent,
    based on the lease document it has access to via vecdb, using RAG.

Run like this:

```
python3 examples/docqa/chat-multi-extract-3.py -m ollama/nous-hermes2-mixtral
```

If you omit the -m arg, it will use the default GPT4-turbo model.

For more on setting up local LLMs with Langroid, see here:
https://langroid.github.io/langroid/tutorials/local-llm-setup/
"""

import typer
from rich import print
from pydantic import BaseModel
from typing import List
import json
import os

import langroid.language_models as lm
from langroid.mytypes import Entity
from langroid.agent.special.doc_chat_agent import DocChatAgent, DocChatAgentConfig
from langroid.parsing.parser import ParsingConfig
from langroid.agent.chat_agent import ChatAgent, ChatAgentConfig
from langroid.agent.task import Task
from langroid.agent.tool_message import ToolMessage
from langroid.language_models.openai_gpt import OpenAIGPTConfig
from langroid.utils.configuration import set_global, Settings
from langroid.utils.constants import NO_ANSWER, DONE

app = typer.Typer()

os.environ["TOKENIZERS_PARALLELISM"] = "false"


class LeasePeriod(BaseModel):
    start_date: str
    end_date: str


class LeaseFinancials(BaseModel):
    monthly_rent: str
    deposit: str


class Lease(BaseModel):
    """
    Various lease terms.
    Nested fields to make this more interesting/realistic
    """

    period: LeasePeriod
    financials: LeaseFinancials
    address: str


class LeaseMessage(ToolMessage):
    """Tool/function to use to present details about a commercial lease"""

    request: str = "lease_info"
    purpose: str = """
        Collect information about a Commercial Lease.
        """
    terms: Lease
    result: str = ""

    def handle(self) -> str:
        print(
            f"""
        DONE! Successfully extracted Lease Info:
        {self.terms}
        """
        )
        return "DONE " + json.dumps(self.terms.dict())

    @classmethod
    def json_instructions(cls, tool: bool = True) -> str:
        instr = super().json_instructions(tool)
        instr += """
        ------------------------------
        ASK ME QUESTIONS ONE BY ONE, to FILL IN THE FIELDS 
        of the `lease_info` function/tool.
        First ask me for the start date of the lease.
        DO NOT ASK ANYTHING ELSE UNTIL YOU RECEIVE MY ANSWER.
        """
        return instr

    @classmethod
    def examples(cls) -> List["LeaseMessage"]:
        return [
            cls(
                terms=Lease(
                    period=LeasePeriod(start_date="2021-01-01", end_date="2021-12-31"),
                    financials=LeaseFinancials(monthly_rent="$1000", deposit="$1000"),
                    address="123 Main St, San Francisco, CA 94105",
                ),
                result="",
            ),
        ]


@app.command()
def main(
    debug: bool = typer.Option(False, "--debug", "-d", help="debug mode"),
    model: str = typer.Option("", "--model", "-m", help="model name"),
    nocache: bool = typer.Option(False, "--nocache", "-nc", help="don't use cache"),
) -> None:
    set_global(
        Settings(
            debug=debug,
            cache=not nocache,
        )
    )
    llm_cfg = OpenAIGPTConfig(
        chat_model=model or lm.OpenAIChatModel.GPT4o,
        chat_context_length=16_000,  # adjust based on model
        temperature=0,
        timeout=45,
    )
    doc_agent = DocChatAgent(
        DocChatAgentConfig(
            llm=llm_cfg,
            n_neighbor_chunks=2,
            parsing=ParsingConfig(
                chunk_size=50,
                overlap=10,
                n_similar_docs=3,
                n_neighbor_ids=4,
            ),
            cross_encoder_reranking_model="",
        )
    )
    doc_agent.vecdb.set_collection("docqa-chat-multi-extract", replace=True)
    print("[blue]Welcome to the real-estate info-extractor!")
    doc_agent.config.doc_paths = [
        "examples/docqa/lease.txt",
    ]
    doc_agent.ingest()
    doc_task = Task(
        doc_agent,
        name="DocAgent",
        done_if_no_response=[Entity.LLM],  # done if null response from LLM
        done_if_response=[Entity.LLM],  # done if non-null response from LLM
        system_message="""You are an expert on Commercial Leases. 
        You will receive various questions about a Commercial 
        Lease contract, along with some excerpts from the Lease.
        Your job is to answer them concisely in at most 2 sentences.
        """,
    )

    lease_extractor_agent = ChatAgent(
        ChatAgentConfig(
            llm=llm_cfg,
            vecdb=None,
        )
    )
    lease_extractor_agent.enable_message(LeaseMessage)

    lease_task = Task(
        lease_extractor_agent,
        name="LeaseExtractorAgent",
        interactive=False,  # set to True to slow it down (hit enter to progress)
        system_message=f"""
        You are an expert at understanding JSON function/tool specifications, and
        you are also very familiar with commercial lease terminology and concepts.
         
        See the `lease_info` function/tool below,  Your FINAL GOAL is to fill
        in the required fields in this `lease_info` function/tool,
        as shown in the example. This is ONLY an EXAMPLE,
        and YOU CANNOT MAKE UP VALUES FOR THESE FIELDS.
        
        To fill in these fields, you must ASK ME QUESTIONS about the lease,
        ONE BY ONE, and I will answer each question. 
        If I am unable to answer your question initially, try asking me 
        differently. If I am still unable to answer after 3 tries, fill in 
        {NO_ANSWER} for that field.
        When you have collected this info, present it to me using the 
        'lease_info' function/tool.
        DO NOT USE THIS Function/tool UNTIL YOU HAVE ASKED QUESTIONS 
        TO FILL IN ALL THE FIELDS.
        
        Think step by step. 
        Phrase each question simply as "What is ... ?",
        and do not explain yourself, or say any extraneous things. 
        Start by asking me for the start date of the lease.
        When you receive the answer, then ask for the next field, and so on.
        """,
    )

    validator_agent = ChatAgent(
        ChatAgentConfig(
            llm=llm_cfg,
            vecdb=None,
            system_message=f"""
            You are obedient, understand instructions, and follow them faithfully,
            paying attention to the FORMAT specified,
            and you are also extremely CONCISE and SUCCINCT in your responses.
            
            Your task is to detect if the user's message is asking for ONE
            piece of information, or MULTIPLE pieces. Here is how you respond:
            
            IF the msg is only asking about ONE thing, OR if it is NOT EVEN a question:
                respond '{DONE}' and say nothing else.

            IF the msg is asking MORE THAN ONE thing,  respond like this:
            "Please only ask ONE question at a time. Try your question again.
            ONLY when you have ALL the answers, then present the info
            using the `lease_info` function/tool."
            """,
        )
    )
    validator_task = Task(
        validator_agent,
        name="Validator",
        single_round=True,
        interactive=False,
    )

    lease_task.add_sub_task([validator_task, doc_task])
    lease_task.run()


if __name__ == "__main__":
    app()

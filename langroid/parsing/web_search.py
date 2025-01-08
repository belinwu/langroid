"""
Utilities for web search.

NOTE: Using Google Search requires setting the GOOGLE_API_KEY and GOOGLE_CSE_ID
environment variables in your `.env` file, as explained in the
[README](https://github.com/langroid/langroid#gear-installation-and-setup).
"""

import os
from typing import Dict, List

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from duckduckgo_search import DDGS
from googleapiclient.discovery import Resource, build
from requests.models import Response


class WebSearchResult:
    """
    Class representing a Web Search result, containing the title, link,
    summary and full content of the result.
    """

    def __init__(
        self,
        title: str,
        link: str,
        max_content_length: int = 3500,
        max_summary_length: int = 300,
    ):
        """
        Args:
            title (str): The title of the search result.
            link (str): The link to the search result.
            max_content_length (int): The maximum length of the full content.
            max_summary_length (int): The maximum length of the summary.
        """
        self.title = title
        self.link = link
        self.max_content_length = max_content_length
        self.max_summary_length = max_summary_length
        self.full_content = self.get_full_content()
        self.summary = self.get_summary()

    def get_summary(self) -> str:
        return self.full_content[: self.max_summary_length]

    def get_full_content(self) -> str:
        try:
            response: Response = requests.get(self.link)
            soup: BeautifulSoup = BeautifulSoup(response.text, "lxml")
            text = " ".join(soup.stripped_strings)
            return text[: self.max_content_length]
        except Exception as e:
            return f"Error fetching content from {self.link}: {e}"

    def __str__(self) -> str:
        return f"Title: {self.title}\nLink: {self.link}\nSummary: {self.summary}"

    def to_dict(self) -> Dict[str, str]:
        return {
            "title": self.title,
            "link": self.link,
            "summary": self.summary,
            "full_content": self.full_content,
        }


def google_search(query: str, num_results: int = 5) -> List[WebSearchResult]:
    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")
    cse_id = os.getenv("GOOGLE_CSE_ID")
    service: Resource = build("customsearch", "v1", developerKey=api_key)
    raw_results = (
        service.cse().list(q=query, cx=cse_id, num=num_results).execute()["items"]
    )

    return [
        WebSearchResult(result["title"], result["link"], 3500, 300)
        for result in raw_results
    ]


def metaphor_search(query: str, num_results: int = 5) -> List[WebSearchResult]:
    """
    Method that makes an API call by Metaphor client that queries
    the top num_results links that matches the query. Returns a list
    of WebSearchResult objects.

    Args:
        query (str): The query body that users wants to make.
        num_results (int): Number of top matching results that we want
            to grab
    """

    load_dotenv()

    api_key = os.getenv("METAPHOR_API_KEY") or os.getenv("EXA_API_KEY")
    if not api_key:
        raise ValueError(
            """
            Neither METAPHOR_API_KEY nor EXA_API_KEY environment variables are set. 
            Please set one of them to your API key, and try again.
            """
        )

    try:
        from metaphor_python import Metaphor
    except ImportError:
        raise ImportError(
            "You are attempting to use the `metaphor_python` library;"
            "To use it, please install langroid with the `metaphor` extra, e.g. "
            "`pip install langroid[metaphor]` or `poetry add langroid[metaphor]` "
            "or `uv add langroid[metaphor]`"
            "(it installs the `metaphor_python` package from pypi)."
        )

    client = Metaphor(api_key=api_key)

    response = client.search(
        query=query,
        num_results=num_results,
    )
    raw_results = response.results

    return [
        WebSearchResult(result.title, result.url, 3500, 300) for result in raw_results
    ]


def duckduckgo_search(query: str, num_results: int = 5) -> List[WebSearchResult]:
    """
    Method that makes an API call by DuckDuckGo client that queries
    the top `num_results` links that matche the query. Returns a list
    of WebSearchResult objects.

    Args:
        query (str): The query body that users wants to make.
        num_results (int): Number of top matching results that we want
            to grab
    """

    with DDGS() as ddgs:
        search_results = [r for r in ddgs.text(query, max_results=num_results)]

    return [
        WebSearchResult(
            title=result["title"],
            link=result["href"],
            max_content_length=3500,
            max_summary_length=300,
        )
        for result in search_results
    ]

"""Looking claims up, so the answer check can rest on evidence and not only on what the left knows.

    HARNESS_LOOKUP=wikipedia      no key; general knowledge
    HARNESS_LOOKUP=searxng        your own SearXNG instance: set HARNESS_LOOKUP_URL
    HARNESS_LOOKUP=brave          Brave Search API: set BRAVE_API_KEY

Off by default. Turning it on sends the answer's factual claims, as search queries, to the
service you choose. Claims are short statements drawn from the answer, which is drawn
from the conversation, so do not turn it on for conversations you would not search for.
"""

import asyncio
import html
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from . import local
from .config import Settings
from .errors import ConfigError
from .schemas import Evidence

USER_AGENT = "hemisphere-harness (https://github.com/Zaenus/hemisphere-harness)"
TIMEOUT_S = 15.0
SNIPPET_CHARS = 700


class Searcher(ABC):
    """Finds evidence for a statement. Never raises for a failed search: it finds nothing."""

    name: str = "search"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, transport=transport)

    async def search(self, query: str, limit: int = 3) -> list[Evidence]:
        try:
            found = await self._search(query, limit)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return []
        return [item.model_copy(update={"snippet": _clean(item.snippet)[:SNIPPET_CHARS]}) for item in found[:limit]]

    async def search_all(self, queries: list[str], limit: int = 3) -> list[list[Evidence]]:
        return list(await asyncio.gather(*(self.search(query, limit) for query in queries)))

    @abstractmethod
    async def _search(self, query: str, limit: int) -> list[Evidence]: ...

    async def aclose(self) -> None:
        await self._client.aclose()


class WikipediaSearcher(Searcher):
    name = "wikipedia"

    def __init__(self, language: str = "en", transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(transport)
        self.endpoint = f"https://{language}.wikipedia.org/w/api.php"

    async def _search(self, query: str, limit: int) -> list[Evidence]:
        # First the pages that match, each with the passage that matched; then their opening
        # sentences. The passage is usually where a specific fact is; the opening gives context.
        # Wikipedia's search wants every word to match, so a long query can find nothing:
        # drop words from the end until something matches.
        words = query.split()
        hits: list[dict[str, Any]] = []
        while words and not hits:
            found = await self._client.get(
                self.endpoint,
                params={"action": "query", "format": "json", "formatversion": "2", "list": "search",
                        "srsearch": " ".join(words), "srlimit": str(limit), "srprop": "snippet"},
            )  # fmt: skip
            found.raise_for_status()
            hits = found.json().get("query", {}).get("search", [])
            words = words[:-1] if len(words) > 2 else []
        if not hits:
            return []
        titles = [hit["title"] for hit in hits]
        pages = await self._client.get(
            self.endpoint,
            params={"action": "query", "format": "json", "formatversion": "2", "titles": "|".join(titles),
                    "prop": "extracts|info", "exintro": "1", "explaintext": "1", "exsentences": "3", "inprop": "url"},
        )  # fmt: skip
        pages.raise_for_status()
        by_title = {page["title"]: page for page in pages.json().get("query", {}).get("pages", [])}
        evidence = []
        for hit in hits:
            page = by_title.get(hit["title"], {})
            passage = _clean(hit.get("snippet", ""))
            opening = page.get("extract", "")
            snippet = f"…{passage}… {opening}" if passage else opening
            url = page.get("fullurl") or f"https://{self.endpoint.split('/')[2]}/wiki/{hit['title'].replace(' ', '_')}"
            evidence.append(Evidence(title=hit["title"], url=url, snippet=snippet))
        return evidence


class SearxngSearcher(Searcher):
    name = "searxng"

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(transport)
        self.base_url = base_url.rstrip("/")

    async def _search(self, query: str, limit: int) -> list[Evidence]:
        response = await self._client.get(f"{self.base_url}/search", params={"q": query, "format": "json"})
        response.raise_for_status()
        results: list[dict[str, Any]] = response.json().get("results", [])
        return [Evidence(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("content", "")) for r in results[:limit]]


class BraveSearcher(Searcher):
    name = "brave"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(transport)
        self._key = api_key

    async def _search(self, query: str, limit: int) -> list[Evidence]:
        response = await self._client.get(
            BRAVE_URL,
            params={"q": query, "count": str(limit)},
            headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
        )
        response.raise_for_status()
        results: list[dict[str, Any]] = response.json().get("web", {}).get("results", [])
        return [Evidence(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("description", "")) for r in results[:limit]]


BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


def searcher_from_settings(settings: Settings) -> Searcher | None:
    """The searcher HARNESS_LOOKUP names, or None if lookups are off."""
    kind = (settings.harness_lookup or "").strip().lower()
    if kind in ("", "off", "none"):
        return None
    if settings.harness_local_only:
        where = {"wikipedia": f"https://{settings.harness_lookup_language or 'en'}.wikipedia.org", "brave": BRAVE_URL}
        url = where.get(kind, settings.harness_lookup_url)
        if url:  # searxng without a URL is refused below, for that reason
            local.require_local(f"HARNESS_LOOKUP={kind}", url)
    if kind == "wikipedia":
        return WikipediaSearcher(settings.harness_lookup_language or "en")
    if kind == "searxng":
        if not settings.harness_lookup_url:
            raise ConfigError("HARNESS_LOOKUP=searxng needs HARNESS_LOOKUP_URL, the address of your SearXNG instance")
        return SearxngSearcher(settings.harness_lookup_url)
    if kind == "brave":
        key = settings.env("BRAVE_API_KEY")
        if not key:
            raise ConfigError("HARNESS_LOOKUP=brave needs BRAVE_API_KEY")
        return BraveSearcher(key)
    raise ConfigError(f"HARNESS_LOOKUP={kind} is not known. Use wikipedia, searxng or brave, or leave it empty.")


def _clean(text: str) -> str:
    """Search snippets without markup, entities or runs of whitespace."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", text)).split())

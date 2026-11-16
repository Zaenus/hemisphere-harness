"""Two built-in tools: searching the web, and reading a page.

    web_search    search HARNESS_LOOKUP's service, or Wikipedia when it is not set
    fetch_url     fetch a page and return its text

Both are read-only: they change nothing, so they run without sign-off and the left may
commit them like any other routine pick. What they return is ordinary tool output: the
left reads it first, and it counts as evidence when the answer is checked.

HARNESS_WEB_SEARCH and HARNESS_FETCH_URL turn each one on; both are on by default, so a
live run can search and read without any further setup. With HARNESS_LOCAL_ONLY, a search
service off this machine leaves web_search out, with the reason in `problems`, and
fetch_url refuses every address off this machine, redirects included.
"""

import codecs
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from . import local
from .config import Settings
from .errors import ConfigError
from .lookup import USER_AGENT, Searcher, WikipediaSearcher
from .tools import Toolbox, ToolResult, ToolSpec

FETCH_TIMEOUT_S = 15.0
MAX_FETCH_BYTES = 500_000
MAX_REDIRECTS = 5
MAX_SEARCH_LIMIT = 10
REDIRECTS = {301, 302, 303, 307, 308}


def search_problem(settings: Settings, *, lookup_configured: bool) -> str | None:
    """Why web_search is left out, when it is on. None when it is offered, or when it is off."""
    if not settings.harness_web_search or lookup_configured or not settings.harness_local_only:
        return None
    return (
        "HARNESS_LOCAL_ONLY is on, and no search service on this machine is available: set "
        "HARNESS_LOOKUP to a SearXNG here, or turn HARNESS_LOCAL_ONLY off."
    )


class BuiltinToolbox(Toolbox):
    """The built-in read-only tools. Create one with `builtin_toolbox(settings, searcher)`.

    `searcher` is shared, not owned: closing this closes it only when `searcher_owned`
    says the toolbox made it.
    """

    def __init__(
        self,
        *,
        searcher: Searcher | None = None,
        searcher_owned: bool = False,
        fetch: bool = False,
        local_only: bool = False,
        problems: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_bytes: int = MAX_FETCH_BYTES,
    ) -> None:
        self.tools = []
        self.problems = dict(problems or {})
        """Built-ins these settings could not offer, and why."""
        self._searcher = searcher
        self._searcher_owned = searcher_owned
        self._local_only = local_only
        self._max_bytes = max_bytes
        self._client: httpx.AsyncClient | None = None
        if searcher is not None:
            self.tools.append(
                ToolSpec(
                    name="web_search",
                    description=(
                        "Search the web for a query and return the matching pages: title, URL and a short "
                        "excerpt of each. Use it for current or specific facts you do not already know; "
                        "fetch_url reads a page in full."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "What to search for."},
                            "limit": {"type": "integer", "description": f"How many pages, 1 to {MAX_SEARCH_LIMIT}."},
                        },
                        "required": ["query"],
                    },
                    source="builtin",
                    read_only=True,
                    needs_approval=False,
                )
            )
        if fetch:
            self.tools.append(
                ToolSpec(
                    name="fetch_url",
                    description=(
                        "Fetch a web page and return its text, to read what a search excerpt only summarises. "
                        "Only http and https pages; long pages are cut short."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "The page's full address, starting http:// or https://."}
                        },
                        "required": ["url"],
                    },
                    source="builtin",
                    read_only=True,
                    needs_approval=False,
                )
            )
            self._client = httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, headers={"User-Agent": USER_AGENT}, transport=transport)

    async def _call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "web_search":
            if self._searcher is None:
                return ToolResult(ok=False, output="web_search is not available")
            return await self._search(self._searcher, arguments)
        if name == "fetch_url":
            return await self._fetch(str(arguments.get("url") or ""))
        return ToolResult(ok=False, output=f"there is no tool named '{name}'")

    async def _search(self, searcher: Searcher, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, output="no query was given")
        try:
            limit = int(arguments.get("limit") or 3)
        except (TypeError, ValueError):
            limit = 3
        found = await searcher.search(query, max(1, min(limit, MAX_SEARCH_LIMIT)))
        if not found:
            return ToolResult(ok=True, output=f"No results for '{query}'.")
        pages = "\n".join(f"{i}. {item.title}\n   {item.url}\n   {item.snippet}" for i, item in enumerate(found, 1))
        return ToolResult(ok=True, output=f"{len(found)} result(s) for '{query}':\n{pages}")

    async def _fetch(self, url: str) -> ToolResult:
        if not url:
            return ToolResult(ok=False, output="no URL was given")
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            return ToolResult(ok=False, output=f"only http and https pages can be fetched, not '{url}'")
        if self._client is None:
            return ToolResult(ok=False, output="fetch_url is not available")
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            refusal = self._refusal(current)
            if refusal:
                return ToolResult(ok=False, output=refusal)
            try:
                async with self._client.stream("GET", current) as response:
                    location = response.headers.get("location", "")
                    if response.status_code in REDIRECTS and location:
                        current = urljoin(str(response.url), location)
                        continue
                    if response.status_code >= 400:
                        return ToolResult(ok=False, output=f"{response.url} answered HTTP {response.status_code}")
                    kind = _content_kind(response.headers.get("content-type", ""))
                    if not _is_text(kind):
                        return ToolResult(ok=False, output=f"{response.url} is not a text page ({kind}).")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > self._max_bytes:
                            break
                    cut = size > self._max_bytes
                    raw = b"".join(chunks)[: self._max_bytes]
                    text = raw.decode(_charset(response.headers.get("content-type", "")), errors="replace")
                    if kind in ("text/html", "application/xhtml+xml"):
                        title, page = _page_text(text)
                    else:
                        title, page = "", _lines(text)
                    header = (f"Title: {title}\n" if title else "") + f"URL: {response.url}"
                    body = f"{header}\n\n{page}" if page else header
                    return ToolResult(ok=True, output=body + ("\n[… page cut short]" if cut else ""))
            except httpx.HTTPError as exc:
                return ToolResult(ok=False, output=f"could not fetch {current}: {type(exc).__name__}: {exc}")
            except (ValueError, httpx.InvalidURL) as exc:  # a URL httpx itself refuses
                return ToolResult(ok=False, output=f"could not fetch {current}: {exc}")
        return ToolResult(ok=False, output=f"more than {MAX_REDIRECTS} redirects fetching {url}")

    def _refusal(self, url: str) -> str | None:
        """The reason this address may not be fetched, or None. HARNESS_LOCAL_ONLY only."""
        if not self._local_only:
            return None
        try:
            local.require_local("the fetch_url tool", url)
        except ConfigError as exc:
            return str(exc)
        return None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._searcher_owned and self._searcher is not None:
            await self._searcher.aclose()


def builtin_toolbox(
    settings: Settings, searcher: Searcher | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> BuiltinToolbox | None:
    """The built-in tools these settings turn on, or None when they turn both off.

    `web_search` searches `searcher` — the answer check's service — when there is one,
    and Wikipedia otherwise, which this then owns and closes. Under HARNESS_LOCAL_ONLY a
    search service off this machine leaves `web_search` out: the reason is in the
    toolbox's `problems`, rather than a run refusing to start over a default setting.
    """
    if not settings.harness_web_search and not settings.harness_fetch_url:
        return None
    problems: dict[str, str] = {}
    chosen: Searcher | None = None
    owned = False
    if settings.harness_web_search:
        if searcher is not None:
            chosen = searcher
        elif (problem := search_problem(settings, lookup_configured=False)) is not None:
            problems["web_search"] = problem
        else:
            chosen = WikipediaSearcher(settings.harness_lookup_language or "en", transport=transport)
            owned = True
    box = BuiltinToolbox(
        searcher=chosen,
        searcher_owned=owned,
        fetch=settings.harness_fetch_url,
        local_only=settings.harness_local_only,
        problems=problems,
        transport=transport,
    )
    return box if box.tools or box.problems else None


class _PageText(HTMLParser):
    """The readable text of a page: nothing from script, style or metadata, the title kept apart."""

    _DROP = {"script", "style", "noscript", "svg", "template"}
    _BLOCK = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "figure",
        "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "p", "pre",
        "section", "table", "td", "th", "tr",
    }  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._skip += 1
            self._in_title = True
        elif tag in self._DROP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._skip = max(0, self._skip - 1)
            self._in_title = False
        elif tag == "head":
            self._skip, self._in_title = 0, False  # an unclosed tag in the head must not swallow the page
        elif tag in self._DROP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def _page_text(html: str) -> tuple[str, str]:
    """A page's title and its readable text. A page so malformed the parser gives up still returns text."""
    parser = _PageText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.title.strip(), _lines("".join(parser.parts))


def _lines(text: str) -> str:
    """One line per line, indentation collapsed, blank lines between blocks kept to one."""
    out: list[str] = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if line:
            out.append(line)
        elif out and out[-1]:
            out.append("")
    return "\n".join(out).strip()


def _content_kind(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower() or "text/html"


def _is_text(kind: str) -> bool:
    return kind.startswith("text/") or kind in ("application/json", "application/xhtml+xml") or kind.endswith("+xml")


def _charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            name = value.strip("\"'")
            try:
                codecs.lookup(name)
            except LookupError:
                return "utf-8"
            return name
    return "utf-8"

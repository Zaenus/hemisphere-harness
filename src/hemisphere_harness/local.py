"""What is running on this machine: local model servers and the models they serve.

The `auto` providers use this to prefer a local model over a hosted one, and
`hemisphere-harness doctor` uses it to show what is available. Every probe is a short
request to a loopback address, and a server that does not answer is simply absent.
"""

import ipaddress
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .errors import ConfigError

PROBE_TIMEOUT_S = 0.5
OLLAMA_URL = "http://localhost:11434"

TRANSPORT: httpx.BaseTransport | None = None
"""Used by every probe that is not given a transport. Tests set it, so that what happens
to be running on the developer's machine never changes a result."""


@dataclass(frozen=True)
class LocalServer:
    """A model server at its usual address. `provider` is its right-hemisphere provider name."""

    provider: str
    url: str
    """The server's root, without /v1."""
    ollama: bool = False
    """Ollama has its own API, which also says which models are decision models."""


SERVERS: tuple[LocalServer, ...] = (
    LocalServer("ollama", OLLAMA_URL, ollama=True),
    LocalServer("lmstudio", "http://localhost:1234"),
    LocalServer("llamacpp", "http://localhost:8080"),
    LocalServer("vllm", "http://localhost:8000"),
)


@dataclass(frozen=True)
class LocalModel:
    id: str
    decision: bool = False
    """Ollama lists "decision" among the model's capabilities: it can be a left hemisphere."""
    size: int | None = None
    """Bytes on disk, where the server says."""
    capabilities: tuple[str, ...] | None = None
    """What Ollama says the model can do. None where the server does not say."""

    @property
    def chat(self) -> bool:
        """Whether it can be a right hemisphere: it writes text, and is not a decision model."""
        if self.capabilities is None:
            return not self.decision
        return "completion" in self.capabilities and not self.decision


@dataclass
class Found:
    server: LocalServer
    models: list[LocalModel] = field(default_factory=list)
    problem: str | None = None
    """Why the server could not be read. None means it answered."""

    @property
    def up(self) -> bool:
        return self.problem is None

    def serves(self, model: str) -> bool:
        return any(same_model(model, m.id) for m in self.models)


def ollama_url() -> str:
    """Ollama's address: OLLAMA_HOST if it is set, as Ollama's own client reads it."""
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if not host:
        return OLLAMA_URL
    if "://" not in host:
        host = f"http://{host}"
    if host.count(":") < 2:  # no port
        host = f"{host}:11434"
    return host.rstrip("/")


def servers() -> tuple[LocalServer, ...]:
    """The servers to look for, with Ollama where OLLAMA_HOST says."""
    url = ollama_url()
    return tuple(LocalServer(s.provider, url, ollama=True) if s.ollama else s for s in SERVERS)


def same_model(wanted: str, served: str) -> bool:
    """Whether a model name names a served model. Ollama's untagged names mean :latest."""

    def norm(name: str) -> str:
        name = name.strip().lower()
        return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"

    return wanted.strip().lower() == served.strip().lower() or norm(wanted) == norm(served)


def probe(
    server: LocalServer,
    *,
    decisions: bool = True,
    timeout_s: float = PROBE_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> Found:
    """Ask one server what it serves. For Ollama, `decisions` also asks which are decision models."""
    found = Found(server)
    try:
        with httpx.Client(timeout=timeout_s, transport=transport or TRANSPORT) as client:
            if server.ollama:
                tags = client.get(f"{server.url}/api/tags")
                tags.raise_for_status()
                for model in tags.json().get("models", []):
                    name = str(model["name"])
                    caps = _capabilities(client, server.url, name) if decisions else None
                    found.models.append(LocalModel(name, "decision" in (caps or ()), model.get("size"), caps))
            else:
                listed = client.get(f"{server.url}/v1/models")
                listed.raise_for_status()
                found.models = [LocalModel(str(model["id"])) for model in listed.json().get("data", [])]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        found.problem = _describe(exc)
        found.models = []
    found.models.sort(key=lambda m: (not m.decision, m.id))
    return found


def scan(
    candidates: Sequence[LocalServer] | None = None,
    *,
    decisions: bool = True,
    timeout_s: float = PROBE_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> list[Found]:
    """Probe every server, in order."""
    return [
        probe(server, decisions=decisions, timeout_s=timeout_s, transport=transport)
        for server in (servers() if candidates is None else candidates)
    ]


def find_right(model: str, *, transport: httpx.BaseTransport | None = None) -> Found | None:
    """The first local server that serves `model`, or None."""
    for server in servers():
        found = probe(server, decisions=False, transport=transport)
        if found.serves(model):
            return found
    return None


def decision_models(url: str | None = None, *, transport: httpx.BaseTransport | None = None) -> list[LocalModel]:
    """The decision models Ollama serves, largest first: the best-measured left comes first."""
    server = LocalServer("ollama", (url or ollama_url()).rstrip("/").removesuffix("/v1"), ollama=True)
    found = probe(server, transport=transport)
    return sorted((m for m in found.models if m.decision), key=lambda m: -(m.size or 0))


def _capabilities(client: httpx.Client, root: str, name: str) -> tuple[str, ...] | None:
    try:
        shown = client.post(f"{root}/api/show", json={"model": name})
        shown.raise_for_status()
        return tuple(str(c) for c in shown.json().get("capabilities", []))
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return None


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "not running"
    if isinstance(exc, httpx.TimeoutException):
        return "did not answer in time"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"answered HTTP {exc.response.status_code}"
    return f"answered something unexpected ({type(exc).__name__})"


# --- HARNESS_LOCAL_ONLY ------------------------------------------------------------------

_HOSTNAME = re.compile(r"[a-z0-9._:-]+")
LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localhost")


def is_local_url(url: str) -> bool:
    """Whether `url` points at this machine or the network it is on.

    Loopback and private addresses, single-label names such as a Docker service
    ("ollama"), and names under a suffix reserved for local networks. Nothing is looked up
    in DNS, so a public name never counts as local, whatever it resolves to.
    """
    try:
        host = (urlsplit(url if "://" in url else f"//{url}").hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if not host or not _HOSTNAME.fullmatch(host):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES)
    return address.is_loopback or address.is_private or address.is_link_local


def require_local(what: str, url: str | None) -> None:
    """Refuse a destination off this machine and its network. For HARNESS_LOCAL_ONLY."""
    if url is None:
        raise ConfigError(f"HARNESS_LOCAL_ONLY is on, and it cannot be told where {what} sends data, so it is refused.")
    if not is_local_url(url):
        host = urlsplit(url if "://" in url else f"//{url}").hostname or url
        raise ConfigError(
            f"HARNESS_LOCAL_ONLY is on, but {what} would send data to {host}, which is not on this "
            "machine or its network. Choose a local one, or turn HARNESS_LOCAL_ONLY off."
        )

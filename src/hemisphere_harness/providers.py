"""Provider registries: how a provider name in the environment becomes a hemisphere.

    RIGHT_PROVIDER=nvidia  ->  right_providers.build("nvidia", settings)  ->  RightHemisphere
    LEFT_PROVIDER=jev      ->  left_providers.build("jev", settings)      ->  LeftHemisphere

A provider is a name and a factory that takes Settings and returns a hemisphere. They
come from three places:

1. Built-ins, registered at the bottom of this module.
2. Your own code, through register_right, register_left or register_openai_compatible.
3. Installed packages that declare an entry point in the group "hemisphere_harness.right"
   or "hemisphere_harness.left". A plugin is imported only when it is asked for by name,
   and it can never take over a name that 1 or 2 already registered.

docs/providers.md walks through adding one.
"""

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Generic, TypeVar

from .config import Settings
from .errors import ConfigError
from .left import ExperimentalLLMLeft, JevLeftHemisphere, LeftHemisphere, MockLeftHemisphere
from .right import OpenAICompatibleRight, RightHemisphere

H = TypeVar("H")


@dataclass(frozen=True)
class Provider(Generic[H]):
    name: str
    build: Callable[[Settings], H]
    summary: str = ""
    aliases: tuple[str, ...] = ()


class Registry(Generic[H]):
    """Named factories for one hemisphere."""

    def __init__(self, kind: str, variable: str, entry_point_group: str) -> None:
        self.kind = kind
        self.variable = variable
        self.entry_point_group = entry_point_group
        self._providers: dict[str, Provider[H]] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        name: str,
        build: Callable[[Settings], H],
        *,
        summary: str = "",
        aliases: tuple[str, ...] = (),
        replace: bool = False,
    ) -> None:
        """Add a provider. Names are case-insensitive. Reusing a name needs replace=True."""
        name = _key(name)
        aliases = tuple(_key(alias) for alias in aliases)
        if not replace:
            for key in (name, *aliases):
                if key in self._providers or key in self._aliases:
                    raise ConfigError(f"a {self.kind}-hemisphere provider named '{key}' already exists")
        self._providers[name] = Provider(name, build, summary, aliases)
        for alias in aliases:
            self._aliases[alias] = name

    def get(self, name: str) -> Provider[H]:
        key = _key(name)
        key = self._aliases.get(key, key)
        if key not in self._providers:
            self._load_plugin(key)
        if key not in self._providers:
            raise ConfigError(
                f"{self.variable}={name} is not a known {self.kind}-hemisphere provider. "
                f"Known: {', '.join(self.names())}. To add one, see docs/providers.md."
            )
        return self._providers[key]

    def build(self, name: str, settings: Settings) -> H:
        return self.get(name).build(settings)

    def names(self) -> list[str]:
        """Every provider name, including plugins that have not been imported yet."""
        return sorted(set(self._providers) | set(self._plugins()))

    def describe(self) -> list[tuple[str, str]]:
        """(name with aliases, summary) for every provider. Imports no plugin."""
        rows = []
        for name in self.names():
            if name in self._providers:
                provider = self._providers[name]
                label = ", ".join((name, *provider.aliases))
                rows.append((label, provider.summary))
            else:
                rows.append((name, f"plugin: {self._plugins()[name].value}"))
        return rows

    def _plugins(self) -> dict[str, EntryPoint]:
        found = {_key(ep.name): ep for ep in entry_points(group=self.entry_point_group)}
        # A plugin cannot shadow a provider that is already registered.
        return {name: ep for name, ep in found.items() if name not in self._providers and name not in self._aliases}

    def _load_plugin(self, key: str) -> None:
        plugin = self._plugins().get(key)
        if plugin is None:
            return
        try:
            build = plugin.load()
        except Exception as exc:  # a broken plugin should say which one it is
            raise ConfigError(f"provider plugin '{key}' ({plugin.value}) failed to load: {exc}") from exc
        self.register(key, build, summary=f"plugin: {plugin.value}")


def _key(name: str) -> str:
    return name.strip().lower()


right_providers: Registry[RightHemisphere] = Registry("right", "RIGHT_PROVIDER", "hemisphere_harness.right")
left_providers: Registry[LeftHemisphere] = Registry("left", "LEFT_PROVIDER", "hemisphere_harness.left")

register_right = right_providers.register
register_left = left_providers.register


def build_right(settings: Settings) -> RightHemisphere:
    """The right hemisphere that RIGHT_PROVIDER names."""
    return right_providers.build(settings.right_provider, settings)


def build_left(settings: Settings) -> LeftHemisphere:
    """The left hemisphere that LEFT_PROVIDER names."""
    return left_providers.build(settings.left_provider, settings)


def register_openai_compatible(
    name: str,
    base_url: str | None,
    *,
    api_key_env: str | None = None,
    aliases: tuple[str, ...] = (),
    replace: bool = False,
) -> None:
    """Register a right-hemisphere provider that speaks OpenAI's chat-completions protocol.

    `base_url` is the provider's endpoint, up to and including any /v1. `api_key_env` is
    the variable its key conventionally lives in; leave it out for servers that need no
    key. Pass base_url=None for a provider with no fixed endpoint, where RIGHT_BASE_URL
    must be set.

    At build time RIGHT_BASE_URL and RIGHT_API_KEY override both. The key from
    `api_key_env` is sent only to `base_url` itself: point RIGHT_BASE_URL somewhere else
    and the provider's key stays home unless RIGHT_API_KEY says otherwise.
    """

    def build(settings: Settings) -> RightHemisphere:
        if not settings.right_model:
            raise ConfigError("no right hemisphere is configured: set RIGHT_MODEL (see .env.example)")
        url = settings.right_base_url or base_url
        if not url:
            raise ConfigError(f"RIGHT_PROVIDER={name} has no endpoint of its own: set RIGHT_BASE_URL")

        api_key = settings.right_api_key.get_secret_value() if settings.right_api_key else None
        at_home = url.rstrip("/") == (base_url or "").rstrip("/")
        if api_key is None and api_key_env and at_home:
            api_key = settings.env(api_key_env)
            if api_key is None:
                raise ConfigError(f"RIGHT_PROVIDER={name} needs an API key: set {api_key_env} or RIGHT_API_KEY")

        return OpenAICompatibleRight(
            settings.right_model,
            base_url=url,
            api_key=api_key,
            name=name,
            timeout_s=settings.right_timeout_s,
            temperature=settings.right_temperature,
            json_mode=settings.right_json_mode,
        )

    if base_url is None:
        summary = "any OpenAI-compatible server · set RIGHT_BASE_URL"
    else:
        summary = f"{base_url} · " + (f"key: {api_key_env}" if api_key_env else "no key")
    register_right(name, build, summary=summary, aliases=aliases, replace=replace)


# --- Built-in right-hemisphere providers -------------------------------------------------
# All of these speak the OpenAI chat-completions protocol. Adding another that does is
# one line here. A provider with a different API subclasses LLMRightHemisphere instead.

register_openai_compatible("openai", "https://api.openai.com/v1", api_key_env="OPENAI_API_KEY")
register_openai_compatible("nvidia", "https://integrate.api.nvidia.com/v1", api_key_env="NVIDIA_API_KEY", aliases=("nim",))
register_openai_compatible("xai", "https://api.x.ai/v1", api_key_env="XAI_API_KEY", aliases=("grok",))
register_openai_compatible("openrouter", "https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY")

# Local servers, at their default ports. None of them needs a key.
register_openai_compatible("ollama", "http://localhost:11434/v1")
register_openai_compatible("lmstudio", "http://localhost:1234/v1", aliases=("lm-studio",))
register_openai_compatible("vllm", "http://localhost:8000/v1")
register_openai_compatible("llamacpp", "http://localhost:8080/v1", aliases=("llama.cpp", "llama-cpp"))

register_openai_compatible("openai-compatible", None, aliases=("custom",))


# --- Built-in left-hemisphere providers --------------------------------------------------

OLLAMA = "http://localhost:11434"
LOCAL_TIMEOUT_S = 120.0  # a local model may have to be loaded into memory by the first call


def _left_api_key(settings: Settings) -> str | None:
    return settings.left_api_key.get_secret_value() if settings.left_api_key else None


def _jev(settings: Settings) -> LeftHemisphere:
    if not settings.jev_api_key:
        raise ConfigError("LEFT_PROVIDER=jev needs JEV_API_KEY (see .env.example)")
    return JevLeftHemisphere(
        settings.jev_api_key.get_secret_value(),
        base_url=settings.jev_base_url,
        model=settings.jev_model,
        timeout_s=settings.jev_timeout_s,
    )


def _system_one(name: str, default_base_url: str | None, example_model: str) -> Callable[[Settings], LeftHemisphere]:
    """A decision model on any server that implements Jev's protocol, POST /v1/systemone."""

    def build(settings: Settings) -> LeftHemisphere:
        if not settings.left_model:
            raise ConfigError(f"LEFT_PROVIDER={name} needs LEFT_MODEL, a decision model such as {example_model}")
        base_url = settings.left_base_url or default_base_url
        if not base_url:
            raise ConfigError(f"LEFT_PROVIDER={name} has no endpoint of its own: set LEFT_BASE_URL")
        return JevLeftHemisphere(
            _left_api_key(settings),
            base_url=base_url,
            model=settings.left_model,
            name=name,
            timeout_s=settings.left_timeout_s or LOCAL_TIMEOUT_S,
        )

    return build


def _llm_experimental(settings: Settings) -> LeftHemisphere:
    if not settings.left_model:
        raise ConfigError("LEFT_PROVIDER=llm-experimental needs LEFT_MODEL, a chat model")
    effort = settings.left_reasoning_effort.strip()
    return ExperimentalLLMLeft(
        settings.left_model,
        base_url=settings.left_base_url or f"{OLLAMA}/v1",
        api_key=_left_api_key(settings),
        reasoning_effort=None if effort.lower() == "omit" else effort,
        timeout_s=settings.left_timeout_s or LOCAL_TIMEOUT_S,
    )


def _mock(settings: Settings) -> LeftHemisphere:
    return MockLeftHemisphere()


def _auto(settings: Settings) -> LeftHemisphere:
    return left_providers.build("jev" if settings.jev_api_key else "mock", settings)


register_left("jev", _jev, summary="TypeSafe AI's hosted Jev · key: JEV_API_KEY")
register_left(
    "ollama",
    _system_one("ollama", OLLAMA, "autotrust-jev"),
    summary=f"a decision model served by Ollama · {OLLAMA}/v1/systemone · no key · set LEFT_MODEL",
)
register_left(
    "systemone",
    _system_one("systemone", None, "the one your server offers"),
    summary="any server that implements /v1/systemone · set LEFT_BASE_URL and LEFT_MODEL",
)
register_left(
    "llm-experimental",
    _llm_experimental,
    summary=f"EXPERIMENTAL, not calibrated · a chat model read through token log-probabilities · {OLLAMA}/v1 · set LEFT_MODEL",
)
register_left("mock", _mock, summary="offline stand-in · no network, not calibrated")
register_left("auto", _auto, summary="jev if JEV_API_KEY is set, otherwise mock")

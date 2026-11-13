"""The settings the web UI can change, and the .env file it keeps them in.

The UI writes to the same .env file that the command-line example reads, so both see the
same configuration. Three rules hold throughout:

- **Secrets are write-only.** Anything ending in _API_KEY is reported as set or not set,
  never returned.
- **The process environment wins.** A variable exported in the shell overrides the file,
  as it does everywhere else in the harness. The UI reports such a variable as coming from
  the environment, so it is clear why a saved value has no effect.
- **Only known variables are written.** Other lines in the file, and their comments, are
  left as they were.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values, set_key, unset_key
from pydantic import ValidationError

from ..config import Settings
from ..errors import ConfigError
from ..providers import left_providers, right_providers

Source = Literal["environment", "file", "default"]


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: Literal["text", "secret", "number", "bool", "provider", "longtext", "choice"] = "text"
    help: str = ""
    aliases: tuple[str, ...] = ()
    choices: tuple[str, ...] = ()


FIELDS: tuple[Field, ...] = (
    # Right hemisphere.
    Field("RIGHT_PROVIDER", "Provider", "provider"),
    Field("RIGHT_MODEL", "Model", help="The model id the provider serves. Pick a strong, long-context model."),
    Field("RIGHT_BASE_URL", "Base URL", help="Overrides the provider's own endpoint, for a proxy or a self-hosted server."),
    Field("RIGHT_API_KEY", "API key override", "secret", help="Sent to whichever endpoint is in use. Usually leave this empty and set the provider's own key."),
    Field("RIGHT_TIMEOUT_S", "Timeout (seconds)", "number"),
    Field("RIGHT_TEMPERATURE", "Temperature", "number", help="Unset by default, because some models reject the parameter."),
    Field("RIGHT_JSON_MODE", "JSON mode", "bool", help="Also sends response_format=json_object. Helps small local models."),
    Field("OPENAI_API_KEY", "OpenAI API key", "secret"),
    Field("NVIDIA_API_KEY", "NVIDIA API key", "secret"),
    Field("XAI_API_KEY", "xAI API key", "secret"),
    Field("OPENROUTER_API_KEY", "OpenRouter API key", "secret"),
    # Left hemisphere.
    Field("LEFT_PROVIDER", "Provider", "provider"),
    Field("LEFT_MODEL", "Model", help="For Ollama, a decision model: ollama show <model> lists \"decision\" under Capabilities."),
    Field("LEFT_BASE_URL", "Base URL", help="Leave empty for the provider's default."),
    Field("LEFT_API_KEY", "API key", "secret"),
    Field("LEFT_TIMEOUT_S", "Timeout (seconds)", "number", help="The first call may load the model. The default for local models is 120."),
    Field("LEFT_REASONING_EFFORT", "Reasoning effort", help="\"none\" stops a reasoning model thinking before it answers. \"omit\" leaves the parameter out."),
    Field("JEV_API_KEY", "Jev API key", "secret", aliases=("TYPESAFE_API_KEY",)),
    Field("JEV_MODEL", "Jev model", aliases=("TYPESAFE_DEFAULT_MODEL",)),
    Field("JEV_BASE_URL", "Jev base URL", aliases=("TYPESAFE_BASE_URL",)),
    Field("JEV_TIMEOUT_S", "Jev timeout (seconds)", "number"),
    # The loop.
    Field("HARNESS_COMMIT_THRESHOLD", "Commit threshold", "number", help="Minimum probability of the left's pick for the left to commit on its own."),
    Field("HARNESS_PERMIT_YES_THRESHOLD", "Policy: allowed at or above", "number"),
    Field("HARNESS_PERMIT_NO_THRESHOLD", "Policy: refused at or below", "number", help="Only a human can approve an option the policy gate refuses."),
    Field("HARNESS_MAX_STEPS", "Maximum steps", "number"),
    Field("HARNESS_POLICY", "Operator policy", "longtext", help="Plain language. Leave empty to turn the policy gate off."),
    Field("HARNESS_CHECK_ANSWERS", "Check answers before showing them", "bool", help="The left judges each factual claim in the answer; the right revises those that do not hold. Off when the left is the mock."),
)  # fmt: skip

FIELDS_BY_NAME = {field.name: field for field in FIELDS}


def _settings_attribute(name: str) -> str:
    return name.lower()


def read(path: Path) -> dict[str, dict[str, Any]]:
    """Every field with its effective value and where it came from. Secrets carry no value."""
    file_values = dotenv_values(path) if path.exists() else {}
    defaults = Settings.model_fields
    out: dict[str, dict[str, Any]] = {}
    for field in FIELDS:
        source: Source = "default"
        value: str | None = None
        for name in (field.name, *field.aliases):
            if os.environ.get(name):
                source, value = "environment", os.environ[name]
                break
        else:
            for name in (field.name, *field.aliases):
                if file_values.get(name):
                    source, value = "file", file_values[name]
                    break

        entry: dict[str, Any] = {"source": source}
        if field.kind == "secret":
            entry["set"] = value is not None
        else:
            entry["value"] = value
            attribute = defaults.get(_settings_attribute(field.name))
            if attribute is not None and attribute.default is not None:
                entry["default"] = str(attribute.default).lower() if isinstance(attribute.default, bool) else str(attribute.default)
        out[field.name] = entry
    return out


def write(path: Path, changes: dict[str, str | None]) -> None:
    """Apply changes to the file: a string sets a variable, None removes it.

    Validates the result as Settings before touching the file, so a bad value is refused
    rather than saved. The file is created readable by its owner only.
    """
    unknown = sorted(set(changes) - set(FIELDS_BY_NAME))
    if unknown:
        raise ConfigError(f"unknown settings: {', '.join(unknown)}")

    cleaned: dict[str, str | None] = {}
    for name, value in changes.items():
        if value is not None:
            value = value.strip()
            if "\n" in value or "\r" in value:
                value = " ".join(value.split())
        if value and "${" in value:
            # python-dotenv expands ${...} even inside quotes, so it would not read back as written.
            raise ConfigError(f"{name}: a .env file cannot hold '${{' literally. Rephrase the value without it.")
        cleaned[name] = value or None

    for name in ("RIGHT_PROVIDER", "LEFT_PROVIDER"):
        if cleaned.get(name):
            registry = right_providers if name == "RIGHT_PROVIDER" else left_providers
            registry.get(cleaned[name] or "")  # raises ConfigError for an unknown name

    with tempfile.TemporaryDirectory() as scratch:
        candidate = Path(scratch) / ".env"
        if path.exists():
            shutil.copyfile(path, candidate)
        _apply(candidate, cleaned)
        try:
            Settings(_env_file=candidate).harness_config()
        except ValidationError as exc:
            raise ConfigError(f"Not saved. {'; '.join(_describe(error) for error in exc.errors())}") from None

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    _apply(path, cleaned)
    os.chmod(path, 0o600)


def _describe(error: Any) -> str:
    """A validation error, named by the variable the user edits."""
    name = "_".join(str(part) for part in error["loc"]).upper()
    if name not in FIELDS_BY_NAME and f"HARNESS_{name}" in FIELDS_BY_NAME:
        name = f"HARNESS_{name}"  # HarnessConfig's fields drop the prefix
    message = str(error["msg"]).removeprefix("Value error, ")
    return f"{name}: {message}" if name else message


def _apply(path: Path, changes: dict[str, str | None]) -> None:
    present = dotenv_values(path)
    for name, value in changes.items():
        if value is None:
            if name in present:
                unset_key(path, name)
        else:
            set_key(path, name, value, quote_mode="always")

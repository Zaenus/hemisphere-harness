"""Configuration: loop thresholds and environment settings.

Turning settings into hemispheres is the job of providers.py.
"""

import os
from pathlib import Path
from typing import Any, Self

from dotenv import dotenv_values
from pydantic import AliasChoices, BaseModel, Field, PrivateAttr, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .left.jev_client import DEFAULT_BASE_URL as JEV_BASE_URL
from .left.jev_client import DEFAULT_MODEL as JEV_MODEL


class HarnessConfig(BaseModel):
    """Thresholds and limits for the loop. Plain data, with no access to the environment."""

    commit_threshold: float = Field(default=0.8, ge=0, le=1)
    """Minimum calibrated probability of the left's pick for the left to commit on its own.

    This is the probability of the chosen option, not Jev's `confidence` field. `confidence`
    measures how concentrated the distribution is, which is not the chance the pick is right.
    """

    permit_yes_threshold: float = Field(default=0.7, ge=0, le=1)
    """At or above this, the policy gate allows an option without sign-off."""

    permit_no_threshold: float = Field(default=0.3, ge=0, le=1)
    """At or below this, the policy gate refuses an option, and only a human can approve it."""

    max_steps: int = Field(default=6, ge=1)

    policy: str = ""
    """Operator policy in plain language. If set, the left gates every option against it."""

    check_answers: bool = True
    """Before the person sees the answer, have the left check its factual claims, and the
    right revise any that do not hold. Claims are judged against the same two thresholds
    as the policy gate."""
    screen_tool_output: bool = True
    """Have the left read every tool's output first. Output that tries to instruct the agent
    reaches the right only quarantined, marked as untrusted data; such lookup results are
    dropped."""
    right_price_input: float | None = Field(default=None, ge=0)
    """Dollars per million input tokens to the right, to show what a run cost. Unset: no cost."""
    right_price_output: float | None = Field(default=None, ge=0)
    """Dollars per million output tokens from the right."""

    @model_validator(mode="after")
    def _permit_thresholds_are_ordered(self) -> Self:
        if self.permit_no_threshold >= self.permit_yes_threshold:
            raise ValueError("permit_no_threshold must be below permit_yes_threshold")
        return self


class Settings(BaseSettings):
    """Everything read from the environment or a .env file. See .env.example."""

    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", populate_by_name=True
    )

    # Right hemisphere. RIGHT_PROVIDER names an entry in providers.right_providers.
    right_provider: str = "auto"
    right_model: str | None = None
    right_base_url: str | None = None
    """Overrides the provider's own endpoint."""
    right_api_key: SecretStr | None = None
    """Overrides the provider's own key variable (OPENAI_API_KEY, NVIDIA_API_KEY, ...)."""
    right_timeout_s: float = 120.0
    right_temperature: float | None = None
    right_json_mode: bool = False
    right_effort: str | None = None
    right_price_input: float | None = None
    right_price_output: float | None = None
    """For Claude: how hard the model works (low, medium, high, xhigh, max). Unset: its default."""

    # Left hemisphere. LEFT_PROVIDER names an entry in providers.left_providers.
    left_provider: str = "auto"
    # For every left provider except TypeSafe's hosted Jev, which has its own block below.
    left_model: str | None = None
    left_base_url: str | None = None
    left_api_key: SecretStr | None = None
    left_timeout_s: float | None = None
    left_reasoning_effort: str = "none"
    """Used by the experimental chat-model left only, which needs the model not to think first."""

    # TypeSafe's hosted Jev. TypeSafe's own variable names work too.
    jev_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("JEV_API_KEY", "TYPESAFE_API_KEY")
    )
    jev_base_url: str = Field(
        default=JEV_BASE_URL, validation_alias=AliasChoices("JEV_BASE_URL", "TYPESAFE_BASE_URL")
    )
    jev_model: str = Field(
        default=JEV_MODEL, validation_alias=AliasChoices("JEV_MODEL", "TYPESAFE_DEFAULT_MODEL")
    )
    jev_timeout_s: float = 10.0

    # Loop.
    harness_commit_threshold: float = 0.8
    harness_permit_yes_threshold: float = 0.7
    harness_permit_no_threshold: float = 0.3
    harness_max_steps: int = 6
    harness_policy: str = ""
    harness_check_answers: bool = True
    harness_screen_tool_output: bool = True
    harness_lookup: str = ""
    """Where the answer check looks claims up: wikipedia, searxng or brave. Empty is off."""
    harness_lookup_url: str | None = None
    harness_lookup_language: str = "en"
    harness_web_search: bool = True
    """Offer the built-in web_search tool, so a run can search while it works.

    It searches the service HARNESS_LOOKUP names, or Wikipedia when that is unset."""
    harness_fetch_url: bool = True
    """Offer the built-in fetch_url tool, so a run can read a page it found."""
    harness_memory: bool = False
    """Keep short notes across conversations. Off by default."""
    harness_memory_file: str | None = None
    """Where the notes are kept. Default: .hemisphere/memory.json."""
    harness_mcp_config: str | None = None
    harness_local_only: bool = False
    """Refuse every endpoint that is not on this machine or its network."""
    harness_notify_url: str | None = None
    harness_notify_on: str = "signoff"
    """A JSON file of MCP servers whose tools runs may use. See mcp_tools.py."""

    _dotenv_files: tuple[Path, ...] = PrivateAttr(default=())

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        # Remember which .env files were read, so env() looks in the same places.
        files = values.get("_env_file", self.model_config.get("env_file"))
        if files is None:
            files = ()
        elif isinstance(files, (str, os.PathLike)):
            files = (files,)
        self._dotenv_files = tuple(Path(file) for file in files)

    def env(self, name: str) -> str | None:
        """A variable these settings do not declare, such as a provider's own API key.

        Provider factories use this instead of os.environ so that a key works the same
        whether it was exported in the shell or written in the .env file. The process
        environment wins.
        """
        value = os.environ.get(name)
        for file in reversed(self._dotenv_files):
            if value:
                break
            value = dotenv_values(file).get(name)
        return value or None

    def harness_config(self) -> HarnessConfig:
        return HarnessConfig(
            commit_threshold=self.harness_commit_threshold,
            permit_yes_threshold=self.harness_permit_yes_threshold,
            permit_no_threshold=self.harness_permit_no_threshold,
            max_steps=self.harness_max_steps,
            policy=self.harness_policy,
            check_answers=self.harness_check_answers,
            screen_tool_output=self.harness_screen_tool_output,
            right_price_input=self.right_price_input,
            right_price_output=self.right_price_output,
        )

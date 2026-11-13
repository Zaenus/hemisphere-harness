"""The web UI: `hemisphere-harness web`. Needs the `web` extra: pip install "hemisphere-harness[web]"."""

from .app import WebConfig, create_app

__all__ = ["WebConfig", "create_app"]

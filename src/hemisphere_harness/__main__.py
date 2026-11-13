"""`python -m hemisphere_harness` is the `hemisphere-harness` command. With no arguments it
lists the providers available in this environment; `web` starts the web UI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

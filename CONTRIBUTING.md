# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate    # or: uv venv && source .venv/bin/activate
pip install -e ".[dev]"                               # or: uv pip install -e ".[dev]"

pytest
python -m hemisphere_harness.examples.simple_loop     # the offline demo
python -m hemisphere_harness                          # lists providers
hemisphere-harness web                                # the web UI
```

The web UI's page is plain HTML, CSS and JavaScript in `src/hemisphere_harness/web/static/`,
with no build step and no dependencies. Edit and reload. Insert anything a model or a
user wrote with `textContent`, never `innerHTML`, and keep everything loaded from the
server itself, because the Content-Security-Policy allows nothing else.

The test suite makes no network calls and needs no credentials. Please keep it that way:
test HTTP clients against `httpx.MockTransport`, as `tests/test_jev_client.py` and
`tests/test_right_openai_compatible.py` do.

Types are checked with `mypy --strict src`. CI runs the tests, the type check and the
offline example on Python 3.11 to 3.14 for every pull request, so run them locally first.

## Measuring the left hemisphere

A change to a left-hemisphere provider, to the questions the harness asks it, or to the
gate's thresholds should come with numbers. Run `hemisphere-harness eval` before and after,
with the left you changed, and put both reports in the pull request.
[docs/evaluation.md](docs/evaluation.md) explains the report and how to add scenarios.

## Adding a provider

This is the most useful kind of contribution, and usually the smallest.
[docs/providers.md](docs/providers.md) has the full guide. In short:

- **An OpenAI-compatible service** is one line in `src/hemisphere_harness/providers.py`,
  one table row, and one test parameter.
- **A service with its own API** is a subclass of `LLMRightHemisphere` with one method,
  `complete`.
- **A provider that pulls in a heavy SDK** is better as its own package, published with
  an entry point, so that this package keeps its short list of small dependencies.

## What the design protects

The project is small on purpose, and its point is a specific relationship between two
parts. Changes are easy to accept when they keep these true, and need a good argument
when they do not. [docs/architecture.md](docs/architecture.md) says where each one is
enforced.

1. Only the right hemisphere generates. The left returns typed answers and nothing else.
2. The left commits only a routine, low-stakes decision that it is sure of.
3. Everything else returns to the right before anything is done.
4. An operator's policy binds both hemispheres.
5. Every step ends with the right reintegrating it.
6. A failing left hemisphere is survivable. A failing right hemisphere is not.

Two practical consequences:

- A feature that lets the left act on more than it does today needs a test showing what
  still stops it.
- The harness reads the left's probabilities as calibrated. A left-hemisphere provider
  whose numbers are not must say so.

## Licence

The project is MIT licensed. By contributing you agree that your contribution is
released under the same licence.

## Pull requests

- Add or update tests for what you change. For a rule in the list above, check that the
  test fails when the rule is broken.
- Keep provider-specific knowledge in the provider's own module. `harness.py` and
  `schemas.py` should not learn any provider's name.
- Update `README.md`, `docs/` and `.env.example` when behaviour or configuration changes.

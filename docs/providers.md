# Providers

A provider is a name and a factory. The name is what you put in `RIGHT_PROVIDER` or
`LEFT_PROVIDER`. The factory takes the settings and returns a hemisphere.

```bash
python -m hemisphere_harness      # lists every provider available in this environment
```

## Right-hemisphere providers

| `RIGHT_PROVIDER` | Endpoint | Key variable |
|---|---|---|
| `openai` (default) | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `nvidia`, `nim` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |
| `xai`, `grok` | `https://api.x.ai/v1` | `XAI_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `ollama` | `http://localhost:11434/v1` | none |
| `lmstudio`, `lm-studio` | `http://localhost:1234/v1` | none |
| `vllm` | `http://localhost:8000/v1` | none |
| `llamacpp`, `llama.cpp`, `llama-cpp` | `http://localhost:8080/v1` | none |
| `openai-compatible`, `custom` | set `RIGHT_BASE_URL` | none, or `RIGHT_API_KEY` |

`RIGHT_MODEL` is always required. No model names are built in, because catalogues change
faster than this file would. Most of these servers list theirs at `GET {endpoint}/models`.

```bash
# NVIDIA
RIGHT_PROVIDER=nvidia
RIGHT_MODEL=<a model id from build.nvidia.com>
NVIDIA_API_KEY=nvapi-...

# Grok
RIGHT_PROVIDER=grok
RIGHT_MODEL=<a model id from docs.x.ai>
XAI_API_KEY=xai-...

# A local model through Ollama: no key, nothing leaves the machine
RIGHT_PROVIDER=ollama
RIGHT_MODEL=<a model you have pulled, as shown by `ollama list`>

# A self-hosted NVIDIA NIM container, or any preset at a different address
RIGHT_PROVIDER=nvidia
RIGHT_BASE_URL=http://localhost:8000/v1

# Any other server that speaks the protocol
RIGHT_PROVIDER=openai-compatible
RIGHT_BASE_URL=https://llm.internal.example/v1
RIGHT_API_KEY=...
```

### How the endpoint and key are chosen

- `RIGHT_BASE_URL`, if set, replaces the provider's endpoint.
- `RIGHT_API_KEY`, if set, is the key that is sent, to whichever endpoint is in use.
- Otherwise the provider's own key variable is used, **and only for the provider's own
  endpoint**. If you point `RIGHT_BASE_URL` somewhere else, `OPENAI_API_KEY` and its
  like are not sent there. Set `RIGHT_API_KEY` if that server needs a key.
- A hosted provider with no key fails at startup with a message naming the variable.
  It does not wait for the first request to return a 401.

Keys can be exported in the shell or written in `.env`. The shell wins.

### Local models

The four local presets need a server you run yourself, with a model loaded. Three things
to know before relying on one as the right hemisphere:

- **Context window.** The prompt carries the goal, the context and the full step
  history, and it grows with every step. Local servers often load a model with a small
  window (Ollama shows it under `CONTEXT` in `ollama ps`) and drop whatever does not
  fit, without an error. Use a model configured with a larger window, which in Ollama
  means a Modelfile with `PARAMETER num_ctx`. Lowering `HARNESS_MAX_STEPS` also keeps
  the prompt shorter.
- **Speed.** A model that does not fit in GPU memory runs partly on the CPU and can take
  a minute or more per call. A step makes two or three calls. If calls time out, raise
  `RIGHT_TIMEOUT_S` (default 120).
- **Structured replies.** The right hemisphere must return valid JSON for a plan, a
  proposal and so on. The client sends a failed reply back once with the validation
  error. If a small model still fails, set `RIGHT_JSON_MODE=true`, which most local
  servers support and which constrains the reply to JSON, or use a larger model.

One observation from testing, with `gemma4` through Ollama over two runs of the example
tickets: every structured reply parsed, but the model labelled nine of its ten proposals
high-stakes, including one that only sent a final confirmation. The left was therefore
allowed to commit once, and every other decision went back to the right. That errs on
the safe side, but it means the left adds little. The labels follow from how the model reads the situation:
given a calm description of the same ticket it labelled the routine one low. Check the
`propose` lines of a trace to see what your model does. This has been observed with one
model, and no fix for it has been tested.

To run a model in-process instead of through a server, write a provider (see below) whose
`complete` calls it.

## Left-hemisphere providers

| `LEFT_PROVIDER` | What it is | Needs |
|---|---|---|
| `auto` (default) | `jev` if `JEV_API_KEY` is set, otherwise `mock` | |
| `jev` | TypeSafe AI's hosted Jev | `JEV_API_KEY` |
| `ollama` | A decision model served by Ollama on your own machine | `LEFT_MODEL` |
| `systemone` | Any other server that implements `POST /v1/systemone` | `LEFT_BASE_URL`, `LEFT_MODEL` |
| `llm-experimental` | **Experimental.** A chat model, read through its token probabilities | `LEFT_MODEL` |
| `mock` | An offline stand-in. Not calibrated. For development and tests. | |

`jev`, `ollama` and `systemone` are the same client pointed at different servers. Jev's
protocol, System One, has become a shared one: a request carries a state and typed
questions, and the reply carries typed answers with probabilities.

### A local decision model through Ollama

Ollama serves decision models at `/v1/systemone`, the same path and shapes as TypeSafe's
API, and needs no key. This is the way to run the whole harness on your own machine.

```bash
ollama pull tev1

LEFT_PROVIDER=ollama
LEFT_MODEL=tev1
```

- **The model must be a decision model.** `ollama show <model>` lists `decision` under
  Capabilities for one. A chat model is refused by the server ("is not a llama.cpp
  decision model"), which the harness treats like any other left failure: the decision
  goes to the right.
- **Models.** Ollama lists them at `ollama.com/search?c=decision`. At the time of
  writing they include `tev1` (0.8 GB and 4.6 GB, from Together AI, marked experimental
  by its authors), `nimble` (9.5 GB, from Bespoke Labs) and `autotrust-jev` (8 to 26 GB,
  from AutoTrust AI). `autotrust-jev` is an open-weights student of Jev 1.13 and is not
  affiliated with TypeSafe. Its model card says it is not for high-stakes decisions and
  that thresholds should be validated on your own data.
- **Check the format before a large download.** `autotrust-jev` is published only in
  MLX format. Ollama on Apple Silicon runs that. On Linux it needs Ollama's MLX runtime,
  which the standard install does not include, and a GPU with enough memory for the
  whole model. `ollama pull` reports this before downloading anything. `tev1` and
  `nimble` are in the llama.cpp format and run wherever your chat models do.
- **The first call loads the model**, which can take a minute. The default timeout is
  120 seconds. Change it with `LEFT_TIMEOUT_S`.
- **Nothing is truncated.** If the state does not fit the model's context window the
  server returns an error, and the decision goes to the right.
- **Ollama on another machine:** `LEFT_BASE_URL=http://gpu-box:11434`.

Tested here with both sizes of `tev1`, on the example's tickets and in fully live runs
with `gemma4` as the right hemisphere, under the example's policy ("Refunds or credits
above $500 need human sign-off. Unlocking accounts and sending replies do not."):

- **`tev1` (4.3B, 4.6 GB)** gave 23 policy answers. None allowed something the policy
  reserves for a human, and none refused something it plainly allows. It allowed
  unlocking accounts (0.99 to 1.00), refused the $2,400 refund (0.03 to 0.04), and refused
  a reply that promised that refund (0.27). Three answers fell in the unsure band: a
  goodwill credit of unstated size (0.46), a message confirming the refund amount (0.35)
  and a plain apology (0.64), which the policy allows. Unsure sends the step to the right,
  so this caution costs a step but not safety. In the live run the right tried to commit
  the reply that promised the refund, and the harness stopped it for a human.
- **`tev1:0.8b`** chose sensibly among options, but its policy answers were unreliable.
  In a live run it allowed the $2,400 refund (0.80) and refused a harmless phone call
  (0.23). Use it to try the loop, not as a gate.

On a laptop GPU with 6 GB, `tev1` answered in 1 to 15 seconds per step, most of it spent
swapping between it and the chat model. Two tickets are not an evaluation. The policy
gate is only as good as the model answering it, so check it against your own policies
before relying on it. `nimble` and `autotrust-jev` have not been run.

### Experimental: a chat model as the left hemisphere

> **Experimental.** Use this only if you cannot run a decision model. Its numbers are
> not calibrated, and the harness's thresholds assume calibration.

```bash
LEFT_PROVIDER=llm-experimental
LEFT_MODEL=<a chat model on your Ollama>

# Another OpenAI-compatible server, if it returns token log-probabilities:
# LEFT_BASE_URL=http://localhost:8000/v1
# LEFT_API_KEY=...
# If the server rejects the reasoning_effort parameter:
# LEFT_REASONING_EFFORT=omit
```

**How it works.** A decision model scores options directly. A chat model can only
generate, so this provider asks it to reply with a single label (a letter for a choice, a
digit for a score, Yes or No), stops it after one token, and reads the probability the
model gave each label. Nothing the model writes is used. A choice is asked once per
rotation of the options, so that each option takes a turn under each letter, and the
results are averaged.

**How you will know it is in use.** The provider's name says so, constructing it raises
an `ExperimentalWarning`, and every `LEFT decide` line of a trace ends the model name
with `, experimental`. `LEFT_PROVIDER=auto` never selects it.

**What it needs.** A server that returns `logprobs` on `/chat/completions`. Ollama does.
A reasoning model must have thinking switched off, otherwise its first token is the
start of its reasoning and the provider reports a failure. `reasoning_effort: "none"` is
sent for that by default.

**What is known to be wrong with it**, from testing two local models:

- *Letter bias.* Asked a two-option question once, one model put 100% on whichever
  option was labelled "B", in either order. Averaging over rotations turns that into
  50/50, which fails the commit threshold as it should. Rotation corrects for position
  and nothing else.
- *Overconfidence.* Answers come out close to 0 or 1 even on judgement calls. In
  practice the provider behaves more like a vote across rotations than like a
  probability: it commits when every rotation agrees, and hesitates when they do not.
  Moving `HARNESS_COMMIT_THRESHOLD` changes little.
- *Yes/no and score questions are not corrected at all.* Asked something unknowable
  ("will it rain on a date years away?"), the two models answered "No" at about 98% and
  100%, where 0.5 would be honest. The policy gate runs on yes/no questions.
- *It is slower than a decision model* for the same questions: one request per question,
  and one per rotation for a choice.

Tested with Ollama only.

## Adding a right-hemisphere provider

### It speaks the OpenAI protocol

Most do. You may not need to add anything: `RIGHT_PROVIDER=openai-compatible` with
`RIGHT_BASE_URL` already works.

To give it a name of its own, register it. In your own code:

```python
from hemisphere_harness import register_openai_compatible

register_openai_compatible("acme", "https://api.acme.example/v1", api_key_env="ACME_API_KEY")
```

To contribute it to this project, add the same line to the built-in list at the bottom
of `src/hemisphere_harness/providers.py`, a row to the table above, and a parameter to
`test_hosted_provider_sends_its_own_key_to_its_own_endpoint` or
`test_local_provider_needs_no_key` in `tests/test_providers.py`.

### It has its own API

Subclass `LLMRightHemisphere` and implement one method. The prompts, the JSON parsing,
the schema validation and the repair attempt are inherited.

```python
import httpx
from hemisphere_harness import (
    ChatMessage, ConfigError, LLMRightHemisphere, RightHemisphereError, Settings, register_right,
)


class AcmeRight(LLMRightHemisphere):
    name = "acme"

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self._client = httpx.AsyncClient(
            base_url="https://api.acme.example", headers={"X-Api-Key": api_key}, timeout=120
        )

    async def complete(self, messages: list[ChatMessage], *, json_reply: bool = False) -> str:
        # messages are {"role": "system" | "user" | "assistant", "content": str}.
        # Translate them into whatever the API expects, and return the reply text.
        try:
            response = await self._client.post("/generate", json={"model": self.model, "turns": messages})
            response.raise_for_status()
            return response.json()["text"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise RightHemisphereError(f"Acme request failed: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def build(settings: Settings) -> AcmeRight:
    key = settings.env("ACME_API_KEY")       # checks the shell, then .env
    if not settings.right_model or not key:
        raise ConfigError("RIGHT_PROVIDER=acme needs RIGHT_MODEL and ACME_API_KEY")
    return AcmeRight(settings.right_model, key)


register_right("acme", build)
```

Three rules for `complete`:

- Raise `RightHemisphereError` on any failure. The harness treats a failing right
  hemisphere as fatal, and that exception is how it knows.
- Return the reply text only. Do not parse it. `json_reply=True` means the caller is
  about to parse it as JSON, so switch on a native JSON mode if the API has one.
- Keep it stateless. The full conversation arrives in `messages` on every call.

If the provider has native structured output and you would rather use that than
prompted JSON, subclass `RightHemisphere` directly and implement its five methods.
`right/prompts.py` is still reusable.

You can also skip the registry entirely. `Harness(AcmeRight(...), left)` works with any
instance. The registry only matters when the provider should be selectable by name
from the environment.

### Shipping it as its own package

A provider does not have to live in this repository. Declare an entry point in your
package, and it becomes selectable as soon as the package is installed:

```toml
# pyproject.toml of hemisphere-harness-acme
[project.entry-points."hemisphere_harness.right"]
acme = "hemisphere_harness_acme:build"
```

`build` is the factory: it takes `Settings` and returns a `RightHemisphere`. The group
for left-hemisphere providers is `hemisphere_harness.left`.

Plugins are imported only when someone selects them by name, so a broken plugin affects
nobody else. A plugin cannot take over a built-in name: if one declares `openai` or
`jev`, the built-in is used.

## Adding a left-hemisphere provider

Subclass `LeftHemisphere` and implement `_decide(state, questions)`, returning a
`LeftResult` with one typed answer per question id. Register it with `register_left`,
or through the `hemisphere_harness.left` entry-point group.

```python
from hemisphere_harness import LeftHemisphere, LeftResult, Settings, register_left


class MyDecisionModel(LeftHemisphere):
    name = "mine"

    async def _decide(self, state, questions) -> LeftResult:
        answers = {qid: ... for qid, question in questions.items()}   # NoulAnswer, ChoiceAnswer, ScoreAnswer
        return LeftResult(answers=answers, model="my-model-v1")


register_left("mine", lambda settings: MyDecisionModel())
```

The base class checks every result: a missing answer, an answer of the wrong type, or a
choice that was never offered is turned into a `LeftHemisphereError`, and the harness
hands the decision to the right.

One requirement is not checked by code, and it is the important one. **The harness
treats the left's probabilities as calibrated.** `HARNESS_COMMIT_THRESHOLD=0.8` means
"let the left act alone when it is right about four times in five". A provider whose
numbers do not mean that, such as a generative LLM asked to state its confidence, will
commit decisions the thresholds were meant to stop. If you wrap one, say so in its
description, and measure its calibration on your own decisions before trusting the
defaults.

## Checklist for a provider pull request

- [ ] Tests run against `httpx.MockTransport` or a stub. Nothing in the suite makes a
      network call or needs a credential.
- [ ] A missing key or model fails at build time, with a message naming the variable.
- [ ] Every failure in the client surfaces as `RightHemisphereError` or
      `LeftHemisphereError`.
- [ ] No model name is hard-coded as a default.
- [ ] The provider tables in this file and in `README.md` have a row for it.
- [ ] `pytest` and `mypy --strict src` pass.

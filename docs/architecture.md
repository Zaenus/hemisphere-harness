# Architecture

## The Master/Emissary contract

Each rule below is enforced in one identifiable place.

| # | Rule | Where it is enforced |
|---|---|---|
| 1 | Only the right generates: plans, options, judgements, text. | `LeftHemisphere` has a single method, `decide`, which returns typed `Answer`s. |
| 2 | The left answers only what it was asked. A missing answer, a wrong type, or a choice that was never offered is a failure. | `LeftHemisphere.decide` wraps every implementation's `_decide` with `validate_answers`. |
| 3 | The left sees a narrow slice and cannot write back. | `WorldState.focus()` builds a fresh dict for each call. |
| 4 | The left commits only a routine, low-stakes pick that it is sure of and that policy allows. | `Harness._gate`, the only path from a left verdict to a commitment. |
| 5 | Everything else returns to the right before anything is done. | `Harness.step` calls `right.evaluate`, then `Harness._resolve`. |
| 6 | The operator's policy binds both hemispheres. An option the gate refused needs a human. | `Harness._resolve`. |
| 7 | Every step ends in the right. Reintegration is the only write to the situation. | The end of `Harness.step`. |
| 8 | A failing left is survivable. A failing right is fatal. | `Harness.step` catches `LeftHemisphereError` only. |
| 9 | Before the person sees the answer, the left checks its claims and the right takes back what does not hold. | `Harness.check_answer`. |
| 10 | A tool that is not read-only is never called without a person's sign-off, whichever hemisphere chose it. | `Harness._gate` and `Harness._resolve`, through `_tool_needing_signoff`. |
| 11 | What the agent remembers across conversations reaches the right only, and only what the left judged relevant. | `Harness._recall`; `WorldState.focus()` leaves memory out. |
| 12 | Tool output the left flags as instructions to the agent reaches the right only fenced and marked untrusted. | `Harness._screen`, `ActionResult.shown()`, `WorldState.observations()`. |
| 13 | With `HARNESS_LOCAL_ONLY`, nothing is sent off this machine and its network. | `local.require_local`, called by `build_right`, `build_left`, `searcher_from_settings`, `MCPToolbox.connect` and the web UI's webhook. |

Rule 4 depends on the right's own labels. A `Proposal` carries `stakes` and `open_ended`,
and neither has a default, so the right must say on every step whether left-hemisphere
precision is enough. If either says no, the left's authority drops from `DECIDE` to
`ADVISE`: it is still consulted, because its probabilities are useful evidence, but it
cannot commit.

## One step

```text
                 ┌────────────────────────────────────────────────────────────┐
                 │ RIGHT  propose(state) → Proposal                           │
                 │        options (2–4), rationale, stakes, open_ended        │
                 └──────────────────────────────┬─────────────────────────────┘
                                                │ state.focus() + typed questions
                 ┌──────────────────────────────▼─────────────────────────────┐
                 │ LEFT   decide(focus, questions) → LeftResult               │
                 │        select: choice     permit:<id>: noul, per option    │
                 └──────────────────────────────┬─────────────────────────────┘
                                                │
                 ┌──────────────────────────────▼─────────────────────────────┐
                 │ HARNESS  _gate                                             │
                 │   routine and low-stakes, p ≥ commit threshold,            │
                 │   and the policy gate allows the pick?                     │
                 └───────┬───────────────────────────────────────────┬────────┘
                     yes │                                           │ no, or the left failed
                         │                    ┌──────────────────────▼────────────────────────┐
                         │                    │ RIGHT  evaluate(state, proposal, decision)    │
                         │                    │        commit(option) │ revise │ ask_human    │
                         │                    └───┬──────────────┬─────────────────┬──────────┘
                         │                 commit │       revise │       ask_human │
                         │        ┌───────────────▼───┐          │                 │
                         │        │ HARNESS _resolve  │          │                 │
                         │        │ known option, and │── no ────┼─────────────────┤
                         │        │ not refused by    │          │                 │
                         │        │ policy?           │          │                 │
                         │        └───────┬───────────┘          │                 │
                         │            yes │                      │                 │
                 ┌───────▼────────────────▼───┐   ┌──────────────▼───┐   ┌─────────▼────────┐
                 │ PROCEED                    │   │ REVISE           │   │ FLAG_HUMAN       │
                 │ act(option) → ActionResult │   │ nothing done     │   │ nothing done     │
                 └───────────────┬────────────┘   └────────┬─────────┘   └─────────┬────────┘
                                 └─────────────────────────┼───────────────────────┘
                 ┌─────────────────────────────────────────▼──────────────────┐
                 │ RIGHT  reintegrate(state, step) → Reintegration            │
                 │        rewritten situation, goal_met                       │
                 └────────────────────────────────────────────────────────────┘
```

`Harness.run` wraps this: `right.plan` once at the start, steps until the goal is met, a
human is needed, or `max_steps` is reached, then `right.generate` for the reply to the
person. That reply (`RunResult.report`) is the whole agent speaking: one voice, in the
first person, answering the request itself. It never describes the hemispheres or the
steps. If something waits on the person, it says what, recommends, and asks. The
instruction is `harness.answer_prompt`.

| After the step | The run |
|---|---|
| `goal_met` is true | ends with `done` |
| outcome is `FLAG_HUMAN` | ends with `needs_human` |
| outcome is `PROCEED` or `REVISE`, goal not met | continues; the next proposal sees the new history |
| `max_steps` reached | ends with `max_steps` |

## The policy gate

If `HarnessConfig.policy` is set, the left is also asked one yes/no question per option:
under this policy, may this action be carried out without human sign-off? The question
adds that approvals mentioned in the request are not sign-off, which is given separately,
and its yes means the policy clearly allows this kind of action. Without that, a request
saying "they confirmed twice" talked a `tev1` gate into allowing an account deletion
([evaluation.md](evaluation.md#how-the-policy-question-was-fixed)). There is one
question per option because Jev answers every question independently against the same
state, so it cannot be asked to gate "whichever option wins". All the questions go in
one request and cost about the same as one.

| Probability the option is allowed | Meaning | Effect |
|---|---|---|
| ≥ `permit_yes_threshold` (0.7) | allowed | no objection |
| between the two | unsure | the left may not commit it; the right may |
| ≤ `permit_no_threshold` (0.3) | refused | neither may commit it; a human must |

The thresholds are the starting points TypeSafe suggests for a noul. A noul has no
separate confidence value: the probability is the whole answer.

## Checking the answer

The run ends with one more pass through both hemispheres, on the answer itself.

1. The right drafts the reply (`generate`).
2. The right lists the draft's factual claims (`claims`): at most eight, each a single
   statement that can be judged on its own. Opinions, advice and statements about what
   the agent did are left out.
3. The left judges every claim in one pass, as yes/no questions. It sees only the request
   and its context, never the right's reading of the situation, so it cannot simply agree
   with the draft.
4. Each claim is placed in the policy gate's bands: holds (≥ 0.7), unsure, doubtful
   (≤ 0.3). If any claim is not in "holds", the right rewrites the reply (`revise`),
   correcting, removing or qualifying it. The check is evidence and the right may keep a
   claim it is sure of, as with any left verdict.

`RunResult.check` keeps the draft and every verdict. The check is best-effort, because an
answer already exists: if listing claims, checking them or revising fails, the draft
stands and the failure is recorded. It is on by default (`HARNESS_CHECK_ANSWERS`). The
example and the web UI turn it off when the left is the mock, which answers every yes/no
question 0.5.

The left judges claims with what it knows, with what the tools returned during the run
(observed, so evidence), and with what the person said earlier in the conversation (never
the agent's own answers). A decision model knows less than a large chat model, so on its
own a "holds" means "not obviously wrong".

**Lookups.** With `Harness(..., searcher=...)` (`HARNESS_LOOKUP`), claims the left did not
judge to hold are searched for, using a short query the right wrote alongside each claim,
and the left judges them again with the evidence in view. `ClaimCheck` keeps the first
probability and the sources. The right revises with the sources and may use what they
say, but still may not add specifics nothing checked. A failed search finds nothing; a
failed second pass keeps the first verdicts.

## Memory

`Harness(..., memory=MemoryStore(path))` keeps notes across conversations. Two calls, one
per hemisphere, in keeping with what each does:

- **Remember, by the right**, after the answer: `RightHemisphere.remember` returns at most
  three short notes, usually none, never anything sensitive, each marked a `preference`
  (how the person likes to work: applies to every request) or a `fact` (matters only when
  a request touches it). Duplicates are skipped.
- **Recall**, at the start of `run`: every preference, and the facts the left judges
  relevant, one yes/no question per fact ("would this help with the request?"), seeing
  only the request. They go into `WorldState.memory`, which only the right sees. If the
  left fails, the five most recent facts are used.

The answer is checked against the preferences too. In the same pass as the claims, the
left gets one question per preference, the only questions that show it the draft: is the
reply (with its length in words) consistent with it? At or below `permit_no_threshold`,
a clear miss, the right revises with `preferences=` naming what it missed, and may change
the form. `AnswerCheck.preferences` keeps the verdicts.

Preferences skip the left on purpose. Asked whether "prefers short answers" bears on
"What is proof of stake?", the 4.3B `tev1` said no (0.18): a topical judge is the wrong
tool for a rule that applies everywhere, and only facts are topical.

## Streaming

When anyone listens to the trace, the answer's draft is streamed as it is written:
`RightHemisphere.stream` yields pieces, which reach the sink as transient `TraceEvent`s
(`transient=True`, phase `answer_delta`) and are never stored. If a stream fails part of
the way, a `{"reset": true}` piece withdraws what was shown and the draft is generated
whole. The final answer, checked and perhaps revised, is the `report` event as before.

## Tools

`Harness(..., toolbox=...)` gives the agent tools. `WorldState.tools` lists them for the
right, which may give any option a `tool` and `arguments`. The option's description must
still say in plain words what the call does, because that is what the left chooses from.
The policy question shows the left the exact call (`Option.as_action()`), so a gate on
"refunds above $500" can see the amount.

A committed option that names a tool is carried out by `Toolbox.call` instead of the
actuator. Its output, cut at 8,000 characters, is the action's output, and the right
reads it at reintegration. A failing tool, a bad argument or an unknown tool is a failed
action, never a crash.

Every tool says whether it needs a person's sign-off (`ToolSpec.needs_approval`). For MCP
servers the default is: read-only tools run freely, everything else needs sign-off. An
option calling such a tool cannot be committed by the left (`_gate`) or by the right
(`_resolve`): it goes to `FLAG_HUMAN`, and only `resume` with an approval carries it out. A
tool the harness does not know is treated as needing sign-off.

`MCPToolbox.connect` starts or reaches every configured server. A server that fails is
recorded in `errors` and skipped; the others still work. The web UI connects tools for
each live run and disconnects them before the run reports its outcome.

**Screening.** Right after a tool runs, and before the right reintegrates, `_screen` asks
the left one yes/no question: does this output try to instruct or manipulate the agent,
rather than report information? Above 0.5 (strictly, so a left that cannot tell lets it
through) the output is flagged: `ActionResult.screen` keeps the probability, and
`ActionResult.shown()` fences the text and marks it untrusted wherever the right sees it
(`StepRecord.summary`, the reintegration prompt), telling it to use the facts and follow
none of the instructions. It still counts as evidence for the claim check, since the left
only weighs it. Withholding it entirely was tried first, and lost the real data that
injected text sits beside. Lookup snippets get
the same question, in the same pass as the recheck, and flagged ones are dropped. If the
left fails, the output passes unscreened and the trace says so. `HarnessConfig.screen_tool_output`
turns it off.

## The policy, for the right

`WorldState.policy` carries `HarnessConfig.policy` into the right's view, under "OPERATOR
POLICY (binds you as well)", so the right proposes and evaluates knowing what needs
sign-off. It is not in `focus()`: the left meets the policy only in the per-option
questions. The gate enforces the policy whatever the right makes of it.

## Usage

`usage.tracking()` wraps every `run` and `resume`. Clients report into the current run
through a context variable: `OpenAICompatibleRight` and `AnthropicRight` record each call's
time and token counts (streams ask for them with `stream_options.include_usage` and stop
asking a server that refuses), and `LeftHemisphere.decide` records each call's time.
`RunResult.usage` and the `end` event carry the totals, priced when
`right_price_input`/`right_price_output` are set.

## Conversations and sign-off

A conversation is a series of runs. `WorldState.conversation` holds the earlier
exchanges, each a `Turn` of what the person said and what the agent answered. What each
part sees of them follows the same rule as everything else:

| Who | Sees |
|---|---|
| the right | the last eight exchanges, answers shortened past 2,000 characters |
| the left, deciding | the last exchange only, clipped to a few hundred characters |
| the left, checking claims | what the person said earlier, never the agent's own answers, which are not evidence |

`Harness.resume(state, decision)` continues a run that stopped for a human. An approval
carries out the chosen option as a step committed by `"human"`. That is the only way past
an option the policy gate refused: it asked for a person's sign-off, and this is it. A
rejection is recorded as a revision with the person's note, and the right proposes
again. Either way the right reintegrates the step, the run continues for up to
`max_steps` more steps, and it answers and checks as usual. Both are traced with the
actor `human`. The stopped run's state is copied, not changed.

`Conversation` wraps both: `send` for a new message, `approve` and `reject` for a
sign-off. The web UI and `hemisphere-harness chat` are built on the same two calls.

## Types

Three layers lead from the left's raw output to the harness's verdict.

| Type | Produced by | Holds |
|---|---|---|
| `NoulQuestion`, `ChoiceQuestion`, `ScoreQuestion` | harness | one typed question |
| `NoulAnswer`, `ChoiceAnswer`, `ScoreAnswer` | left | one answer with probabilities |
| `LeftResult` | left | all answers from one pass, plus the model and latency |
| `Decision` | harness | outcome, authority, what the left said, who committed what, and the reasons |

The right produces `Plan`, `Proposal` (with `Option`s), `Evaluation` and `Reintegration`.
The harness records each step as a `StepRecord` in `WorldState.history` and returns a
`RunResult` with the final state, the report and the trace.

### Harness names and Jev's wire format

The harness uses its own field names. `left/jev_client.py` is the only module that knows
Jev's. Ollama's `/v1/systemone` uses the same request and answer shapes, so the same
client serves local decision models.

| Harness | Jev request | Jev response |
|---|---|---|
| `ChoiceQuestion.options` (`{id: description}`) | `criteria` object | `choice`, `confidence`, `probabilities` |
| `ScoreQuestion.levels` (lowest first) | `criteria` array | `score`, `confidence`, `legend`, `probabilities` keyed `"0"`, `"1"`, … |
| `NoulQuestion.yes_when`, `.no_when` | `criteria.true`, `criteria.false` | `noul` |
| `NoulAnswer.probability` | | `noul` |
| `ChoiceAnswer.probability` | | `probabilities[choice]` |

`ChoiceAnswer.confidence` is the server's own statistic. Jev computes
`(p_max − 1/n) / (1 − 1/n)` and Ollama computes `1 − H(p) / ln(n)`. Both measure how
concentrated the distribution is, and neither is the chance of being right. The gate uses `probability` instead,
because that is the calibrated chance that the pick is right.

## Errors

| Failure | Raised as | What the harness does |
|---|---|---|
| Jev timeout, connection error, 408, 429, 5xx | retried, then `LeftHemisphereError` | decision goes to the right |
| Jev 4xx (bad key, invalid request) | `LeftHemisphereError`, no retry | decision goes to the right; the trace shows why |
| Jev answer missing, mistyped, or outside the options | `LeftHemisphereError` | decision goes to the right |
| Left's pick below the commit threshold | not an error | decision goes to the right |
| LLM timeout, connection error, 408, 429, 5xx | retried, then `RightHemisphereError` | run ends |
| LLM reply is not valid JSON for the schema | one repair attempt, then `RightHemisphereError` | run ends |
| Right commits to an option that was not offered | not an error | flagged for a human |
| The actuator raises | captured in `ActionResult(ok=False)` | the right reintegrates the failure |

A bad Jev key does not stop a run. Every step shows `LEFT decide unavailable: …` in the
trace and is decided by the right.

## Local first

The default providers, `auto` on both sides, look on this machine first (`local.py`).
The right uses the first local server (Ollama, LM Studio, llama.cpp, vLLM) that serves
`RIGHT_MODEL`; the left uses the largest decision model Ollama serves. Hosted services
are the fallback, never the first choice. Every probe goes to a loopback address with a
half-second timeout, and a server that does not answer is simply absent.

`HARNESS_LOCAL_ONLY` makes it a rule. "Local" means loopback and private addresses,
single-label names (a Docker service such as `ollama`), and names under `.local`,
`.lan`, `.internal` and `.home.arpa`. Nothing is resolved through DNS, so a public name
never passes, whatever it points to. The check runs where each connection is configured:

| What | Refused how |
|---|---|
| a hemisphere | `build_right` / `build_left` raise `ConfigError`. A hemisphere that does not expose a `base_url` (a plugin, say) is refused, since where it sends data cannot be told. The offline mocks pass. |
| lookups | `searcher_from_settings` raises: Wikipedia and Brave always, SearXNG unless its URL is local. |
| MCP servers | one reached by a URL that is not local is skipped and recorded in `errors`, like a server that failed. Servers started as a local command are allowed; what they reach is up to them. |
| the webhook | not called, and a warning is logged. |

`hemisphere-harness doctor` lists every destination a run would use and marks the ones
that leave the machine.

## Providers

Neither hemisphere is tied to a vendor. `providers.py` keeps one registry per hemisphere,
mapping a name to a factory that takes `Settings` and returns a hemisphere.
`build_right` and `build_left` look up `RIGHT_PROVIDER` and `LEFT_PROVIDER` there.

```text
RightHemisphere                 the contract: plan, propose, evaluate, reintegrate, generate
└── LLMRightHemisphere          all five from one method, complete(messages) -> str
    └── OpenAICompatibleRight   complete() over POST /chat/completions
                                presets: openai, nvidia, xai, openrouter, ollama, lmstudio, …

LeftHemisphere                  the contract: decide(state, questions) -> typed answers
├── JevLeftHemisphere           POST /v1/systemone: TypeSafe's hosted Jev, or a decision
│                               model served by Ollama. Providers: jev, ollama, systemone
├── ExperimentalLLMLeft         a chat model read through token probabilities. Not
│                               calibrated. Provider: llm-experimental
└── MockLeftHemisphere
```

Names resolve in a fixed order: built-ins and anything registered in code first, then
entry-point plugins. A plugin is imported only when selected, and it cannot replace a
name that is already registered.

The harness itself never sees a provider name. It is handed two objects and holds them
to the two contracts above.

## Extension points

**Another right-hemisphere provider.** For any model that turns a chat into text,
subclass `LLMRightHemisphere` and implement `complete(messages) -> str`. The prompts,
JSON extraction, validation and repair are inherited. A provider with native structured
output can subclass `RightHemisphere` and implement its five methods directly.
Registering the provider makes it selectable through `RIGHT_PROVIDER`; a package can
register one through an entry point. See [providers.md](providers.md).

**Another left hemisphere.** Subclass `LeftHemisphere` and implement `_decide`. The base
class validates whatever it returns. Register it with `register_left` to select it
through `LEFT_PROVIDER`. A generative LLM could be wrapped this way as a fallback, but
its probabilities would not be calibrated, and the thresholds assume they are.

**Tools.** Pass `act=` to `Harness`. It receives the committed `Option` and the state and
returns a string describing what happened. For a registry of tools, add an action name
and arguments to `Option`, tell the right what is available through `context`, and
dispatch inside the actuator. Choosing among known tools is the kind of decision the
left is for.

**More questions for the left.** Override `Harness.build_questions` to add scores or
further gates to the same pass. Every answer is kept on `StepRecord.left`. To let a new
answer affect who commits, override `_gate` too.

**Interfaces.** Pass `on_event=` to `Harness` to receive every `TraceEvent` as it
happens. `summary` and `details` are text for a terminal, which is what `TracePrinter`
prints. `data` carries the same event as JSON-ready structures: the plan, the proposal
and the left's authority, the full `Decision` with its probabilities and permits, the
evaluation, the action and the reintegration. The web UI (`src/hemisphere_harness/web/`)
is built on this alone. It streams events to the browser over server-sent events and
draws them from `data`. It does not reach into the harness.

**Human in the loop.** On `needs_human`, `result.state.history[-1]` holds the proposal,
the left's verdict and the right's recommendation. `Harness.resume(result.state,
HumanDecision(...))` continues that run with a person's decision. See below.

**Memory.** `WorldState` is a Pydantic model, so it serialises as it stands. Load earlier
situations or lessons into `context` at the start of a run. Have the right decide what
is worth keeping, at reintegration.

**Multiple agents.** A `Harness` is a self-contained async object with no global state.
An actuator can start another `Harness` for a sub-goal and return its report, which
keeps one right hemisphere responsible for each whole.

## Choices made in this version

- **The gate thresholds on the pick's probability, not on Jev's `confidence`.** See above.
- **`evaluate` and `reintegrate` are separate.** Both return the left's work to the right,
  at different moments. `evaluate` happens before commitment and only when context
  matters. `reintegrate` happens after every step and is the only thing that updates
  the situation.
- **The right is not asked to review a routine, confident pick.** That fast path is the
  point of having a left hemisphere. The right still sees the result at reintegration.
- **Structured replies are requested in the prompt** and validated with Pydantic, so the
  right-hemisphere client needs nothing beyond `model` and `messages`.
- **A left failure never stops a run.** This includes a bad API key, which shows up in
  the trace on every step.
- **Stopping for a human ends the run.** There is no callback that waits for an answer.
  `resume` picks it up later from the saved state, in the same process or another.
- **A reply is a new run, not a resumption.** It plans afresh with the conversation in
  view. Only an explicit approval or rejection continues a stopped run, so a casual
  "yes" can never carry out an action the policy reserved for a person.
- **The person hears one agent.** The two hemispheres are how the work is organised, not
  who the person talks to. As in McGilchrist's cycle, what the left settled is taken back
  into the right, and the right speaks for the whole. The trace keeps the division of
  labour for anyone who wants to inspect it.

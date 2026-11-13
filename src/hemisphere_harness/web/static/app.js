// Hemisphere Harness web UI. Plain ES module, no build step, no dependencies.
//
// Everything a model or a user wrote is inserted as text (textContent), never as HTML.

"use strict";

// ---------------------------------------------------------------------------------------
// DOM helpers

const PROPS = new Set(["value", "checked", "disabled", "hidden", "selected", "open", "type", "placeholder"]);

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (PROPS.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

const ICONS = {
  plus: ["M12 5v14", "M5 12h14"],
  settings: ["M4 6h9", "M17 6h3", "M4 12h3", "M11 12h9", "M4 18h11", "M19 18h1",
    "M17 6a2 2 0 1 1-4 0a2 2 0 1 1 4 0", "M11 12a2 2 0 1 1-4 0a2 2 0 1 1 4 0", "M19 18a2 2 0 1 1-4 0a2 2 0 1 1 4 0"],
  stop: ["M7 7h10v10H7z"],
  trash: ["M4 7h16", "M9 7V4h6v3", "M6 7l1 13h10l1-13"],
  again: ["M4 12a8 8 0 0 1 14-5.3", "M20 4v5h-5", "M20 12a8 8 0 0 1-14 5.3", "M4 20v-5h5"],
  eye: ["M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z", "M15 12a3 3 0 1 1-6 0a3 3 0 1 1 6 0"],
  eyeOff: ["M3 3l18 18", "M10.6 6.1A10 10 0 0 1 12 6c6 0 10 6 10 6a17 17 0 0 1-3 3.5", "M6.6 6.6C3.8 8.4 2 12 2 12s4 7 10 7a9.6 9.6 0 0 0 5.4-1.6"],
  x: ["M6 6l12 12", "M18 6L6 18"],
  menu: ["M4 6h16", "M4 12h16", "M4 18h16"],
  check: ["M5 12l5 5 9-11"],
  alert: ["M12 3l10 18H2z", "M12 10v4", "M12 17.5v.5"],
  info: ["M12 3a9 9 0 1 1 0 18a9 9 0 1 1 0-18", "M12 11v6", "M12 7.5v.5"],
  chevron: ["M9 6l6 6-6 6"],
  play: ["M8 5l11 7-11 7z"],
  search: ["M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14", "M20 20l-4-4"],
  bolt: ["M13 2L4 14h7l-1 8 9-12h-7z"],
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name] || []) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

function toast(message, kind = "") {
  const el = h("div", { class: `toast ${kind}` }, message);
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
}

function relativeTime(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

const pct = (p) => `${Math.max(0, Math.min(100, p * 100))}%`;
const fmt = (p) => (p === null || p === undefined ? "–" : Number(p).toFixed(2));

// ---------------------------------------------------------------------------------------
// API

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    init.headers["content-type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  let data = null;
  try {
    data = await response.json();
  } catch {
    // not JSON
  }
  if (response.status === 401) {
    showSignedOut();
    throw new ApiError("Signed out", 401);
  }
  if (!response.ok) throw new ApiError((data && data.error) || `${response.status} ${response.statusText}`, response.status);
  return data;
}

function showSignedOut() {
  const main = document.getElementById("main");
  clear(main).append(
    h("div", { class: "page" },
      h("div", { class: "notice warn" }, icon("alert"),
        h("div", { class: "grow" },
          h("strong", {}, "This session has ended. "),
          "Open the link that ",
          h("code", {}, "hemisphere-harness web"),
          " printed in your terminal. A new link is made each time the server starts.")))
  );
}

// ---------------------------------------------------------------------------------------
// State

const state = {
  status: null,
  runs: [],
  run: null,
  source: null,
  view: null,
  prefill: null,
};

const PROVIDER_NAMES = {
  openai: "OpenAI",
  nvidia: "NVIDIA NIM",
  xai: "xAI · Grok",
  openrouter: "OpenRouter",
  ollama: "Ollama",
  lmstudio: "LM Studio",
  vllm: "vLLM",
  llamacpp: "llama.cpp",
  "openai-compatible": "Custom endpoint",
  auto: "Automatic",
  jev: "Jev · TypeSafe AI",
  systemone: "Other System One server",
  "llm-experimental": "Chat model",
  mock: "Mock",
};

const LEFT_NAMES = { ollama: "Ollama decision model" };
const providerName = (name, side) => (side === "left" && LEFT_NAMES[name]) || PROVIDER_NAMES[name] || name;

// ---------------------------------------------------------------------------------------
// Sidebar

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch {
    return;
  }
  const s = state.status;
  const box = clear(document.getElementById("hemi-status"));
  box.append(
    h("div", { class: "row", title: s.right.problem || s.right.label },
      h("span", { class: `dot ${s.right.ready ? "right" : "off"}` }),
      h("span", {}, "Right"),
      h("span", {}, s.right.ready ? s.right.label : "not set up")),
    h("div", { class: "row", title: s.left.problem || s.left.label },
      h("span", { class: `dot ${s.left.ready ? "left" : "off"}` }),
      h("span", {}, "Left"),
      h("span", {}, s.left.ready ? s.left.label + (s.left.experimental ? " (experimental)" : "") : "not set up")),
  );
}

async function refreshRuns() {
  try {
    const data = await api("/api/runs");
    state.runs = data.runs;
  } catch {
    return;
  }
  renderRunList();
}

function renderRunList() {
  const list = clear(document.getElementById("run-list"));
  if (!state.runs.length) {
    list.append(h("div", { class: "empty" }, "No runs yet."));
    return;
  }
  const current = state.run && state.run.id;
  for (const run of state.runs) {
    list.append(
      h("button", {
        class: `run-item ${run.id === current ? "active" : ""}`,
        type: "button",
        onclick: () => { location.hash = `#/run/${run.id}`; closeSidebar(); },
      },
        h("span", { class: `dot ${run.status}` }),
        h("span", {},
          h("div", { class: "goal" }, run.goal),
          h("div", { class: "when" }, relativeTime(run.created), run.demo ? " · demo" : ""))),
    );
  }
}

function closeSidebar() {
  document.getElementById("sidebar").classList.remove("open");
}

// ---------------------------------------------------------------------------------------
// Composer: start a run

function showComposer(prefill = null) {
  closeStream();
  state.run = null;
  state.view = "composer";
  renderRunList();
  const s = state.status || { right: {}, left: {} };
  const rightReady = !!s.right.ready;
  const values = prefill || state.prefill || {};
  state.prefill = null;

  const goal = h("textarea", {
    class: "input goal", rows: 3, placeholder: "Describe the goal. For example: Resolve support ticket #5120…",
    value: values.goal || "", "aria-label": "Goal",
  });
  const context = h("textarea", {
    class: "input", rows: 4, placeholder: "Facts the hemispheres should know: account details, constraints, what has happened so far.",
    value: values.context || "", "aria-label": "Context",
  });
  const policy = h("textarea", {
    class: "input", rows: 2, placeholder: "No policy: the gate is off.",
    value: values.policy !== undefined ? values.policy : s.policy || "", "aria-label": "Operator policy",
  });
  const maxSteps = h("input", { class: "input", type: "number", min: 1, max: 50, value: values.max_steps || s.max_steps || 6, "aria-label": "Maximum steps" });
  const policyNote = h("div", { class: "field-note" });
  const updatePolicyNote = () => {
    policyNote.textContent = s.left.mock && policy.value.trim()
      ? "The left is the mock, which answers every policy question 0.5, so every step will go to the right."
      : "";
  };
  policy.addEventListener("input", updatePolicyNote);
  updatePolicyNote();

  const start = h("button", { class: "btn primary", type: "button", disabled: !rightReady }, icon("play"), "Start run");
  start.addEventListener("click", async () => {
    start.disabled = true;
    try {
      const run = await api("/api/runs", {
        method: "POST",
        body: { goal: goal.value, context: context.value, policy: policy.value, max_steps: Number(maxSteps.value) || null },
      });
      location.hash = `#/run/${run.id}`;
      refreshRuns();
    } catch (error) {
      if (error.status !== 401) toast(error.message, "error");
      start.disabled = !rightReady;
    }
  });
  goal.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !start.disabled) start.click();
  });

  const demo = (key, title, description) =>
    h("button", { class: "card demo", type: "button", onclick: () => startDemo(key) },
      h("div", { class: "t" }, title),
      h("div", { class: "d" }, description),
      h("div", { class: "chips" },
        rightReady ? h("span", { class: "chip right" }, "live, with your models") : h("span", { class: "chip" }, "offline, scripted")));

  const notices = [];
  if (!rightReady) {
    notices.push(h("div", { class: "notice warn" }, icon("alert"),
      h("div", { class: "grow" },
        h("strong", {}, "The right hemisphere is not set up. "),
        s.right.problem ? `${s.right.problem} ` : "",
        "The offline demos below still work."),
      h("button", { class: "btn small", type: "button", onclick: () => openSettings("right") }, "Set up")));
  }
  if (s.left.experimental) {
    notices.push(h("div", { class: "notice warn" }, icon("alert"),
      h("div", { class: "grow" }, h("strong", {}, "Experimental left hemisphere. "),
        "A chat model's token probabilities are not calibrated, and the thresholds assume they are.")));
  }

  const main = clear(document.getElementById("main"));
  main.append(
    h("div", { class: "page" },
      h("div", { class: "hero" },
        h("h1", {}, "What should the harness work on?"),
        h("p", {}, "The right hemisphere plans and proposes. The left decides what it may. Everything returns to the right.")),
      h("div", { class: "stack" }, notices),
      h("div", { class: "card composer" },
        goal,
        h("details", { class: "more", open: !!(values.context || values.policy) },
          h("summary", {}, icon("chevron"), "Context, policy and limits"),
          h("div", { class: "extras" },
            h("label", { class: "field" }, h("span", { class: "field-label" }, "Context"), context),
            h("label", { class: "field" }, h("span", { class: "field-label" }, "Operator policy"), policy,
              h("span", { class: "field-help" }, "Plain language. The left checks every option against it, and an option it refuses always needs a human."),
              policyNote),
            h("label", { class: "field" }, h("span", { class: "field-label" }, "Maximum steps"), maxSteps))),
        h("div", { class: "foot" },
          h("span", { class: "faint" }, "Actions are simulated. Nothing touches a real system."),
          start)),
      h("div", { class: "section-title" }, "Or try a demo ticket"),
      h("div", { class: "demos" },
        demo("lockout", "Locked-out customer", "Routine. The left commits a step on its own, then hands a near-tie back to the right."),
        demo("refund", "$2,400 refund", "High stakes. The left only advises, and the policy requires a human to sign off.")),
    ),
  );
  if (rightReady) goal.focus();
}

async function startDemo(key) {
  try {
    const run = await api("/api/runs", { method: "POST", body: { demo: key } });
    location.hash = `#/run/${run.id}`;
    refreshRuns();
  } catch (error) {
    if (error.status !== 401) toast(error.message, "error");
  }
}

// ---------------------------------------------------------------------------------------
// Run view

const PHASES = {
  plan: "Plans",
  propose: "Proposes options",
  decide: "Decides",
  gate: "Gate",
  evaluate: "Evaluates",
  act: "Acts",
  reintegrate: "Reintegrates",
  report: "Report",
  end: "End",
};
const WHO = { right: "Right · Master", left: "Left · Emissary", harness: "Harness" };
const STATUS = {
  running: "Running",
  done: "Done",
  needs_human: "Needs you",
  max_steps: "Step limit reached",
  failed: "Failed",
  cancelled: "Stopped",
};

function closeStream() {
  if (state.source) {
    state.source.close();
    state.source = null;
  }
}

async function showRun(id) {
  closeStream();
  state.view = "run";
  let run;
  try {
    run = await api(`/api/runs/${encodeURIComponent(id)}`);
  } catch (error) {
    if (error.status === 404) {
      toast("That run no longer exists.", "error");
      location.hash = "#/";
    }
    return;
  }
  state.run = run;
  renderRunList();
  const view = new RunView(run);
  clear(document.getElementById("main")).append(view.root);
  if (run.status === "running") view.listen();
}

class RunView {
  constructor(run) {
    this.run = run;
    this.steps = new Map();
    this.timeline = h("div", { class: "timeline" });
    this.working = h("div", { class: "working" });
    this.footer = h("div", {});
    this.head = h("div", { class: "run-head" });
    this.root = h("div", { class: "page" }, this.head, this.timeline, this.working, this.footer);
    this.renderHead();
    for (const event of run.events) this.add(event);
    this.renderEnd();
  }

  renderHead() {
    const run = this.run;
    const running = run.status === "running";
    const c = run.config;
    clear(this.head).append(
      h("div", { class: "chips" },
        h("span", { class: `chip status-${run.status}` }, running ? h("span", { class: "dot running" }) : null, STATUS[run.status] || run.status),
        h("span", { class: "chip right", title: "Right hemisphere" }, run.right),
        h("span", { class: "chip left", title: "Left hemisphere" }, run.left),
        run.left_experimental ? h("span", { class: "chip warn" }, "experimental left") : null,
        run.policy ? h("span", { class: "chip harness", title: run.policy }, "policy on") : h("span", { class: "chip" }, "no policy"),
        h("span", { class: "chip", title: "Commit threshold · policy thresholds · step limit" },
          `commit ≥ ${fmt(c.commit_threshold)} · max ${c.max_steps} steps`),
        h("span", { class: "chip" }, "simulated actions")),
      h("h1", {}, run.goal),
      run.context ? h("details", { class: "more" }, h("summary", {}, icon("chevron"), "Context"), h("div", { class: "context-box stack" }, run.context)) : null,
      run.policy ? h("details", { class: "more" }, h("summary", {}, icon("chevron"), "Operator policy"), h("div", { class: "context-box stack" }, run.policy)) : null,
      h("div", { class: "actions" },
        running
          ? h("button", { class: "btn small danger", type: "button", onclick: () => this.stop() }, icon("stop"), "Stop")
          : h("button", { class: "btn small", type: "button", onclick: () => this.again() }, icon("again"), "Run again"),
        running ? null : h("button", { class: "btn small ghost", type: "button", onclick: () => this.remove() }, icon("trash"), "Delete")),
    );
  }

  listen() {
    const after = this.run.events.length - 1;
    const source = new EventSource(`/api/runs/${encodeURIComponent(this.run.id)}/events?after=${after}`);
    state.source = source;
    this.setWorking();
    source.addEventListener("trace", (message) => {
      const event = JSON.parse(message.data);
      this.run.events.push(event);
      this.add(event);
      this.setWorking();
    });
    source.addEventListener("end", (message) => {
      Object.assign(this.run, JSON.parse(message.data));
      source.close();
      if (state.source === source) state.source = null;
      this.renderHead();
      this.renderEnd();
      refreshRuns();
    });
    source.addEventListener("error", () => {
      if (source.readyState === EventSource.CLOSED) {
        clear(this.working).append(h("span", { class: "faint" }, "Lost the connection to the server. Reload to reconnect."));
      }
    });
  }

  async stop() {
    try {
      await api(`/api/runs/${encodeURIComponent(this.run.id)}/cancel`, { method: "POST" });
    } catch (error) {
      if (error.status !== 401) toast(error.message, "error");
    }
  }

  again() {
    const run = this.run;
    if (run.demo) {
      startDemo(run.demo);
      return;
    }
    state.prefill = { goal: run.goal, context: run.context, policy: run.policy, max_steps: run.config.max_steps };
    location.hash = "#/new";
  }

  async remove() {
    if (!confirm("Delete this run from the history?")) return;
    try {
      await api(`/api/runs/${encodeURIComponent(this.run.id)}`, { method: "DELETE" });
      location.hash = "#/";
      refreshRuns();
    } catch (error) {
      if (error.status !== 401) toast(error.message, "error");
    }
  }

  container(step) {
    if (step === null || step === undefined) return this.timeline;
    if (!this.steps.has(step)) {
      const group = h("div", { class: "timeline" });
      this.timeline.append(h("div", { class: "step-head" }, `Step ${step}`), group);
      this.steps.set(step, group);
    }
    return this.steps.get(step);
  }

  add(event) {
    if (event.phase === "end") return; // shown by renderEnd
    const card = renderEvent(event, this.run);
    this.container(event.step).append(card);
  }

  setWorking() {
    clear(this.working);
    if (this.run.status !== "running") return;
    const [actor, text] = nextActivity(this.run);
    this.working.append(h("span", { class: `dot ${actor}` }), text);
  }

  renderEnd() {
    clear(this.working);
    clear(this.footer);
    if (this.run.status === "running") {
      this.setWorking();
      return;
    }
    const run = this.run;
    const end = run.events.find((event) => event.phase === "end");
    const steps = end ? end.data.steps : run.events.filter((e) => e.phase === "reintegrate").length;
    const titles = {
      done: "The right judged the goal met.",
      needs_human: "Stopped for a human. Nothing in the flagged step was done.",
      max_steps: "Stopped at the step limit.",
      failed: "The run failed.",
      cancelled: "Stopped before it finished.",
    };
    const banner = h("div", { class: `end-banner ${run.status}` },
      icon(run.status === "done" ? "check" : run.status === "failed" ? "alert" : "info"),
      h("div", { class: "grow" },
        h("h3", {}, titles[run.status] || run.status),
        h("div", { class: "muted" }, `${steps} step${steps === 1 ? "" : "s"}`, run.finished ? ` · finished ${relativeTime(run.finished)}` : ""),
        run.error ? h("div", { class: "output" }, run.error) : null,
        run.status === "needs_human" ? signOff(run) : null));
    this.footer.append(banner);
  }
}

function nextActivity(run) {
  const events = run.events;
  const last = events[events.length - 1];
  if (!last) return ["right", "The right hemisphere is planning…"];
  const step = last.step || 0;
  switch (last.phase) {
    case "plan":
      return ["right", "The right is proposing options for step 1…"];
    case "propose":
      return ["left", "The left is deciding…"];
    case "decide":
    case "evaluate":
      return ["harness", "Gating…"];
    case "gate": {
      const outcome = last.data.decision && last.data.decision.outcome;
      if (outcome === "escalate_to_right") return ["right", "The right is evaluating the left's verdict…"];
      if (outcome === "proceed") return ["harness", `Carrying out '${last.data.decision.option_id}'…`];
      return ["right", `The right is reintegrating step ${step}…`];
    }
    case "act":
      return ["right", `The right is reintegrating step ${step}…`];
    case "reintegrate": {
      const gate = [...events].reverse().find((e) => e.phase === "gate" && e.step === step);
      const flagged = gate && gate.data.decision && gate.data.decision.outcome === "flag_human";
      const met = last.data.reintegration && last.data.reintegration.goal_met;
      if (flagged || met || step >= run.config.max_steps) return ["right", "The right is writing its report…"];
      return ["right", `The right is proposing options for step ${step + 1}…`];
    }
    default:
      return ["harness", "Finishing…"];
  }
}

function renderEvent(event, run) {
  const d = event.data || {};
  const head = (meta) =>
    h("div", { class: "event-head" },
      h("span", { class: `who ${event.actor}` }, WHO[event.actor] || event.actor),
      h("span", { class: "phase" }, PHASES[event.phase] || event.phase),
      meta ? h("span", { class: "event-meta" }, meta) : null);
  const card = (meta, ...body) => h("div", { class: `event ${event.actor}` }, head(meta), body);

  switch (event.phase) {
    case "plan": {
      const plan = d.plan || { situation: event.summary, steps: [] };
      return card(null,
        h("div", { class: "text" }, plan.situation),
        plan.steps.length ? h("ol", {}, plan.steps.map((s) => h("li", {}, s))) : null);
    }
    case "propose": {
      const p = d.proposal;
      if (!p) return fallback(event, card);
      const decides = d.authority === "decide";
      return card(null,
        h("div", { class: "chips" },
          h("span", { class: `chip ${p.stakes === "high" ? "warn" : "ok"}` }, `${p.stakes} stakes`),
          h("span", { class: "chip" }, p.open_ended ? "open-ended" : "routine"),
          h("span", { class: `chip ${decides ? "left" : "right"}` }, decides ? "the left may decide" : "the left only advises")),
        h("div", { class: "sub" }, p.rationale),
        h("div", { class: "options" }, p.options.map((o) =>
          h("div", { class: "option" }, h("code", {}, o.id), h("span", { class: "desc" }, o.description)))));
    }
    case "decide": {
      if (d.error !== undefined) {
        return card(null, h("div", { class: "notice danger" }, icon("alert"),
          h("div", { class: "grow" }, h("strong", {}, "The left hemisphere is unavailable. "), "The right decides this step alone.",
            h("div", { class: "output" }, d.error))));
      }
      const decision = d.decision;
      if (!decision) return fallback(event, card);
      const meta = `${d.model}${d.latency_ms != null ? ` · ${Math.round(d.latency_ms)} ms` : ""} · ${d.questions} question${d.questions === 1 ? "" : "s"}`;
      const c = run.config;
      const ranked = Object.entries(decision.probabilities).sort((a, b) => b[1] - a[1]);
      const choice = h("div", { class: "bars" }, ranked.map(([id, p]) =>
        barRow(id, p, id === decision.left_choice, "", [c.commit_threshold])));
      const parts = [
        h("div", { class: "bars-title" }, "Which option? ", h("span", { class: "faint" }, `(marker: commit threshold ${fmt(c.commit_threshold)})`)),
        choice,
      ];
      const permits = Object.entries(decision.permits || {});
      if (permits.length) {
        parts.push(
          h("div", { class: "bars-title" }, "May it be done without sign-off? ",
            h("span", { class: "faint" }, `(refused ≤ ${fmt(c.permit_no_threshold)} · allowed ≥ ${fmt(c.permit_yes_threshold)})`)),
          h("div", { class: "bars" }, permits.map(([id, p]) => {
            const band = p >= c.permit_yes_threshold ? "allowed" : p <= c.permit_no_threshold ? "refused" : "unsure";
            return barRow(id, p, id === decision.left_choice, band, [c.permit_no_threshold, c.permit_yes_threshold], band);
          })));
      }
      return card(meta, parts);
    }
    case "gate": {
      const decision = d.decision;
      if (!decision) return fallback(event, card);
      // A gate that resolves the right's commit brings no new reason; the earlier ones are on the first gate.
      const reasons = d.new_reasons || (decision.outcome === "proceed" ? [] : decision.reasons || []);
      const label = {
        proceed: decision.committed_by === "left" ? `The left commits '${decision.option_id}'` : `The right commits '${decision.option_id}'`,
        escalate_to_right: "Back to the right",
        flag_human: "Flagged for a human",
        revise: "Revise: nothing done",
      }[decision.outcome] || event.summary;
      const iconName = { proceed: "check", flag_human: "alert", escalate_to_right: "again", revise: "x" }[decision.outcome] || "info";
      return card(null,
        h("span", { class: `verdict ${decision.outcome}` }, icon(iconName), label),
        reasons.length ? h("ul", { class: "reasons" }, reasons.map((r) => h("li", {}, r))) : null);
    }
    case "evaluate": {
      const e = d.evaluation;
      if (!e) return fallback(event, card);
      const verdict = { commit: `Commit '${e.option_id}'`, revise: "Revise the options", ask_human: "Ask a human" }[e.verdict] || e.verdict;
      return card(null, h("div", { class: "chips" }, h("span", { class: "chip right" }, verdict)), h("div", { class: "sub" }, e.reasoning));
    }
    case "act": {
      const a = d.action;
      if (!a) return fallback(event, card);
      return card("simulated",
        h("div", {}, h("code", {}, a.option_id), a.ok ? "" : h("span", { class: "chip danger" }, "failed")),
        h("div", { class: "output" }, a.output));
    }
    case "reintegrate": {
      const r = d.reintegration;
      if (!r) return fallback(event, card);
      return card(null,
        h("div", { class: "text" }, r.situation),
        h("div", { class: "chips spaced" }, h("span", { class: `chip ${r.goal_met ? "ok" : ""}` }, r.goal_met ? "goal met" : "goal not yet met")));
    }
    case "report": {
      const text = d.report !== undefined ? d.report : event.summary;
      return card(null, h("div", { class: "report" }, text.split(/\n{2,}/).map((p) => h("p", { class: "text" }, p))));
    }
    default:
      return fallback(event, card);
  }
}

function fallback(event, card) {
  return card(null, h("div", { class: "text" }, event.summary), event.details.map((line) => h("div", { class: "sub" }, line)));
}

function barRow(id, p, top, fillClass, markers, band) {
  const fill = h("div", { class: `bar-fill ${fillClass}` });
  fill.style.width = pct(p);
  const track = h("div", { class: "bar-track" }, fill);
  for (const m of markers) {
    const marker = h("div", { class: "marker", title: `threshold ${fmt(m)}` });
    marker.style.left = `calc(${pct(m)} - 1px)`;
    track.append(marker);
  }
  return h("div", { class: `bar-row ${top ? "top" : ""}`, title: band ? `${id}: ${band}` : id },
    h("span", { class: "name" }, id), track, h("span", { class: "val" }, fmt(p)));
}

function signOff(run) {
  const gate = [...run.events].reverse().find((e) => e.phase === "gate" && e.data.decision && e.data.decision.outcome === "flag_human");
  if (!gate) return null;
  const step = gate.step;
  const propose = run.events.find((e) => e.phase === "propose" && e.step === step);
  const evaluation = run.events.find((e) => e.phase === "evaluate" && e.step === step);
  const decision = gate.data.decision;
  const recommended = evaluation && evaluation.data.evaluation ? evaluation.data.evaluation.option_id : null;
  const options = propose && propose.data.proposal ? propose.data.proposal.options : [];

  const followUp = (text) => {
    state.prefill = {
      goal: run.goal,
      context: `${run.context ? run.context + "\n\n" : ""}${text}`,
      policy: run.policy,
      max_steps: run.config.max_steps,
    };
    location.hash = "#/new";
  };

  return h("div", { class: "signoff" },
    h("div", {}, h("strong", {}, `Step ${step} is waiting for you. `), decision.reasons[decision.reasons.length - 1] || ""),
    options.map((o) => {
      const permit = decision.permits ? decision.permits[o.id] : undefined;
      return h("div", { class: `option ${o.id === recommended ? "chosen" : ""}` },
        h("code", {}, o.id),
        h("span", { class: "desc" }, o.description,
          o.id === recommended ? h("span", { class: "chip right" }, "the right recommends") : null,
          o.id === decision.left_choice ? h("span", { class: "chip left" }, `the left's pick, p=${fmt(decision.probability)}`) : null,
          permit !== undefined ? h("span", { class: "chip" }, `policy ${fmt(permit)}`) : null),
        h("button", { class: "btn small tag", type: "button", onclick: () => followUp(`Human sign-off on step ${step}: approved '${o.id}' (${o.description}). It may be carried out.`) }, "Approve"));
    }),
    h("div", { class: "row" },
      h("button", { class: "btn small", type: "button", onclick: () => followUp(`Human sign-off on step ${step}: none of the offered options (${options.map((o) => o.id).join(", ")}) is approved. Find another way.`) }, "Reject all"),
      h("span", { class: "faint" }, "Starts a new run with your decision added to its context. The harness has no resume yet.")));
}

// ---------------------------------------------------------------------------------------
// Settings

const SECTIONS = [
  ["right", "Right hemisphere"],
  ["left", "Left hemisphere"],
  ["loop", "Loop and policy"],
  ["about", "About"],
];

const settings = {
  data: null,
  draft: {},
  section: "right",
  picker: null,
  status: { text: "", kind: "" },
};

function fieldInfo(name) {
  return settings.data.fields.find((f) => f.name === name);
}

function current(name) {
  if (name in settings.draft) return settings.draft[name];
  const f = fieldInfo(name);
  return f && f.kind !== "secret" ? f.value : null;
}

async function openSettings(section = settings.section) {
  settings.section = section;
  settings.draft = {};
  settings.picker = null;
  settings.status = { text: "", kind: "" };
  try {
    settings.data = await api("/api/settings");
  } catch (error) {
    if (error.status !== 401) toast(error.message, "error");
    return;
  }
  document.getElementById("settings").hidden = false;
  if (!location.hash.startsWith("#/run/")) history.replaceState(null, "", `#/settings/${section}`);
  renderSettings();
}

function closeSettings() {
  if (Object.keys(settings.draft).length && !confirm("Discard unsaved changes?")) return;
  document.getElementById("settings").hidden = true;
  settings.draft = {};
  if (location.hash.startsWith("#/settings")) history.replaceState(null, "", "#/");
}

function renderSettings() {
  const modal = clear(document.querySelector("#settings .modal"));
  const nav = h("nav", { class: "modal-nav" }, h("h2", { id: "settings-title" }, "Settings"),
    SECTIONS.map(([key, label]) =>
      h("button", {
        class: `nav-item ${settings.section === key ? "active" : ""}`, type: "button",
        onclick: () => { settings.section = key; settings.picker = null; renderSettings(); },
      }, label)));

  const body = { right: rightSection, left: leftSection, loop: loopSection, about: aboutSection }[settings.section]();
  const dirty = Object.keys(settings.draft).length > 0;
  const statusText = h("span", { class: `status-text ${settings.status.kind}` }, settings.status.text);
  const testable = settings.section === "right" || settings.section === "left";

  modal.append(nav,
    h("div", { class: "modal-body" },
      h("div", { class: "modal-top" },
        h("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: closeSettings }, icon("x"))),
      h("div", { class: "modal-scroll" }, body),
      h("div", { class: "modal-foot" },
        statusText,
        testable ? h("button", { class: "btn", type: "button", onclick: () => saveSettings(settings.section) }, icon("bolt"), dirty ? "Save and test" : "Test connection") : null,
        h("button", { class: "btn", type: "button", disabled: !dirty, onclick: () => { settings.draft = {}; settings.status = { text: "", kind: "" }; renderSettings(); } }, "Cancel"),
        h("button", { class: "btn primary", type: "button", disabled: !dirty, onclick: () => saveSettings() }, "Save"))));
}

async function saveSettings(testSide = null) {
  const dirty = Object.keys(settings.draft).length > 0;
  try {
    if (dirty) {
      settings.data = await api("/api/settings", { method: "PUT", body: settings.draft });
      settings.draft = {};
      settings.status = { text: `Saved to ${settings.data.env_file}`, kind: "ok" };
      refreshStatus().then(() => { if (state.view === "composer") showComposer(); });
    }
    if (testSide) {
      settings.status = { text: "Testing…", kind: "" };
      renderSettings();
      const result = await api("/api/check", { method: "POST", body: { hemisphere: testSide } });
      settings.status = { text: result.message, kind: result.ok ? "ok" : "error" };
    }
  } catch (error) {
    if (error.status === 401) return;
    settings.status = { text: error.message, kind: "error" };
  }
  renderSettings();
}

function sourceBadge(f) {
  if (!f || f.source !== "environment") return null;
  return h("span", { class: "source", title: "Exported in the shell that started the server. It overrides the settings file." }, "from your shell");
}

function textField(name, { label, placeholder, help, type = "text", rows = 0 } = {}) {
  const f = fieldInfo(name);
  const locked = f.source === "environment";
  const value = current(name);
  const attrs = {
    class: "input", id: `f-${name}`, placeholder: placeholder !== undefined ? placeholder : f.default || "",
    value: value === null || value === undefined ? "" : String(value), disabled: locked,
    oninput: (event) => { settings.draft[name] = event.target.value.trim() === "" ? null : event.target.value; markDirty(); },
  };
  const input = rows ? h("textarea", { ...attrs, rows }) : h("input", { ...attrs, type });
  return h("div", { class: "field" },
    h("label", { class: "field-label", for: `f-${name}` }, label || f.label, sourceBadge(f)),
    input,
    help !== undefined ? (help ? h("span", { class: "field-help" }, help) : null) : f.help ? h("span", { class: "field-help" }, f.help) : null);
}

function boolField(name) {
  const f = fieldInfo(name);
  const value = current(name);
  const on = String(value === null || value === undefined ? f.default : value).toLowerCase() === "true";
  return h("div", { class: "field" },
    h("label", { class: "toggle" },
      h("input", { type: "checkbox", checked: on, disabled: f.source === "environment",
        onchange: (event) => { settings.draft[name] = event.target.checked ? "true" : "false"; markDirty(); } }),
      h("span", {}, f.label), sourceBadge(f)),
    f.help ? h("span", { class: "field-help" }, f.help) : null);
}

function secretField(name, label) {
  const f = fieldInfo(name);
  const locked = f.source === "environment";
  const removing = settings.draft[name] === null;
  const placeholder = locked ? "Set in your shell" : removing ? "Will be removed when you save" : f.set ? "Saved. Leave blank to keep it." : "Enter your API key";
  const input = h("input", {
    class: "input", id: `f-${name}`, type: "password", autocomplete: "off", spellcheck: "false",
    placeholder, disabled: locked, value: typeof settings.draft[name] === "string" ? settings.draft[name] : "",
    oninput: (event) => {
      if (event.target.value) settings.draft[name] = event.target.value;
      else delete settings.draft[name];
      markDirty();
    },
  });
  const eye = h("button", { class: "icon-btn", type: "button", "aria-label": "Show secret", disabled: locked }, icon("eye"));
  eye.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    eye.setAttribute("aria-label", show ? "Hide secret" : "Show secret");
    clear(eye).append(icon(show ? "eyeOff" : "eye"));
  });
  return h("div", { class: "field" },
    h("label", { class: "field-label", for: `f-${name}` }, label || f.label, h("code", { class: "faint" }, name), sourceBadge(f)),
    h("div", { class: "input-row" },
      h("div", { class: "input-wrap" }, input, eye),
      f.set && f.source === "file" && !removing
        ? h("button", { class: "btn small ghost", type: "button", onclick: () => { settings.draft[name] = null; renderSettings(); } }, "Remove")
        : null),
    h("span", { class: "field-help" },
      locked ? "Exported in your shell, which overrides the settings file." : "Write-only: the page never shows a saved key."));
}

function markDirty() {
  const foot = document.querySelector("#settings .modal-foot");
  if (!foot) return;
  const dirty = Object.keys(settings.draft).length > 0;
  const buttons = foot.querySelectorAll("button");
  buttons.forEach((button) => {
    if (button.textContent === "Save" || button.textContent === "Cancel") button.disabled = !dirty;
    if (button.textContent.includes("test") || button.textContent.includes("Test")) {
      const label = dirty ? "Save and test" : "Test connection";
      clear(button).append(icon("bolt"), label);
    }
  });
}

function providerTiles(side, providers, selected) {
  const variable = side === "right" ? "RIGHT_PROVIDER" : "LEFT_PROVIDER";
  const pick = (name) => {
    settings.draft[variable] = name;
    settings.picker = null;
    renderSettings();
  };
  const tile = (p) =>
    h("button", { class: `tile ${p.name === selected ? "active" : ""}`, type: "button", onclick: () => pick(p.name),
      disabled: fieldInfo(variable).source === "environment" },
      h("span", { class: "n" }, providerName(p.name, side), p.experimental ? h("span", { class: "chip warn" }, "experimental") : null),
      h("span", { class: "s" }, tileSummary(p, side)));

  if (side === "right") {
    const order = ["openai", "nvidia", "xai", "openrouter", "ollama", "lmstudio", "vllm", "llamacpp", "openai-compatible"];
    providers = [...providers].sort((a, b) => (order.indexOf(a.name) + 1 || 99) - (order.indexOf(b.name) + 1 || 99));
    const groups = [
      ["Hosted", providers.filter((p) => p.base_url && !p.base_url.startsWith("http://localhost"))],
      ["On your machine", providers.filter((p) => p.base_url && p.base_url.startsWith("http://localhost"))],
      ["Other", providers.filter((p) => !p.base_url)],
    ];
    return groups.filter(([, list]) => list.length).map(([title, list]) =>
      h("div", {}, h("div", { class: "tile-group" }, title), h("div", { class: "tiles" }, list.map(tile))));
  }
  const order = ["auto", "jev", "ollama", "systemone", "llm-experimental", "mock"];
  const sorted = [...providers].sort((a, b) => (order.indexOf(a.name) + 1 || 99) - (order.indexOf(b.name) + 1 || 99));
  return h("div", { class: "tiles" }, sorted.map(tile));
}

function tileSummary(p, side) {
  if (p.plugin) return "plugin";
  if (side === "right") {
    if (!p.base_url) return "any OpenAI-compatible server";
    const host = p.base_url.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    return p.api_key_env ? host : `${host} · no key`;
  }
  return {
    auto: "Jev if its key is set, otherwise the mock",
    jev: "hosted · needs JEV_API_KEY",
    ollama: "local decision model · no key",
    systemone: "any /v1/systemone server",
    "llm-experimental": "not calibrated",
    mock: "offline stand-in",
  }[p.name] || p.summary;
}

function modelField(side, provider) {
  const name = side === "right" ? "RIGHT_MODEL" : provider.name === "jev" ? "JEV_MODEL" : "LEFT_MODEL";
  const canList = side === "right" || ["ollama", "llm-experimental", "systemone"].includes(provider.name);
  const field = textField(name, {
    label: "Model",
    placeholder: fieldInfo(name).default || "Type the model id, or fetch the list",
  });
  if (!canList) return field;
  const fetchButton = h("button", { class: "link-btn", type: "button" }, "Fetch available models");
  fetchButton.addEventListener("click", async () => {
    fetchButton.disabled = true;
    fetchButton.textContent = "Fetching…";
    const baseVar = side === "right" ? "RIGHT_BASE_URL" : "LEFT_BASE_URL";
    const keyVar = side === "right" ? provider.api_key_env || "RIGHT_API_KEY" : "LEFT_API_KEY";
    try {
      const data = await api("/api/models", {
        method: "POST",
        body: {
          hemisphere: side,
          provider: provider.name,
          base_url: typeof settings.draft[baseVar] === "string" ? settings.draft[baseVar] : null,
          api_key: typeof settings.draft[keyVar] === "string" ? settings.draft[keyVar] : null,
        },
      });
      settings.picker = { side, name, models: data.models, query: "" };
    } catch (error) {
      if (error.status === 401) return;
      settings.picker = { side, name, error: error.message, models: [], query: "" };
    }
    renderSettings();
  });
  const label = field.querySelector(".field-label");
  label.append(h("span", { class: "grow" }), fetchButton);
  label.style.justifyContent = "space-between";
  if (settings.picker && settings.picker.name === name) field.append(modelPicker(name, provider));
  return field;
}

function modelPicker(name, provider) {
  const picker = settings.picker;
  const search = h("input", { class: "input", type: "search", placeholder: "Search models", value: picker.query, "aria-label": "Search models" });
  const list = h("div", { class: "picker-list" });
  const fill = () => {
    clear(list);
    if (picker.error) {
      list.append(h("div", { class: "picker-empty" }, picker.error));
      return;
    }
    const q = picker.query.toLowerCase();
    const models = picker.models.filter((m) => m.id.toLowerCase().includes(q));
    if (!models.length) {
      list.append(h("div", { class: "picker-empty" }, picker.models.length ? "No match." : "The server lists no models. Type the id instead."));
      return;
    }
    for (const m of models) {
      const notDecision = provider.name === "ollama" && m.decision === false;
      list.append(h("button", {
        class: "picker-item", type: "button", disabled: notDecision,
        title: notDecision ? "Not a decision model. Ollama refuses it at /v1/systemone." : m.id,
        onclick: () => { settings.draft[name] = m.id; settings.picker = null; renderSettings(); },
      }, h("span", {}, m.id),
        m.decision === true ? h("span", { class: "chip left" }, "decision") : notDecision ? h("span", { class: "chip" }, "chat") : null));
    }
  };
  search.addEventListener("input", () => { picker.query = search.value; fill(); });
  fill();
  setTimeout(() => search.focus(), 0);
  return h("div", { class: "picker" }, search, list);
}

function rightSection() {
  const data = settings.data;
  const selectedName = current("RIGHT_PROVIDER") || fieldInfo("RIGHT_PROVIDER").default || "openai";
  const provider = data.right_providers.find((p) => p.name === selectedName || (p.aliases || []).includes(selectedName)) || { name: selectedName, settings: [] };
  const fields = [];
  if (!provider.plugin) {
    fields.push(modelField("right", provider));
    if (provider.api_key_env) fields.push(secretField(provider.api_key_env, `${providerName(provider.name, "right")} API key`));
    fields.push(textField("RIGHT_BASE_URL", {
      label: "Base URL",
      placeholder: provider.base_url || "https://your-server.example/v1 (required)",
      help: provider.base_url ? "Leave empty to use the provider's own endpoint." : "The server's OpenAI-compatible endpoint, up to and including /v1.",
    }));
  }
  return h("div", {},
    h("div", { class: "settings-title" }, "Right hemisphere · the Master"),
    h("p", { class: "settings-sub" }, "A generative LLM that plans, proposes the options, judges what the left may not, and folds every step back into the whole. Pick a strong, long-context model."),
    providerTiles("right", data.right_providers, provider.name),
    h("div", { class: "provider-card" },
      h("div", { class: "head" }, h("span", { class: "n" }, providerName(provider.name, "right")), h("code", { class: "faint" }, provider.name)),
      provider.plugin ? h("p", { class: "muted" }, "A plugin provider. Configure it through the variables its documentation names.") : null,
      h("div", { class: "stack" }, fields,
        provider.plugin ? null : h("details", { class: "more" }, h("summary", {}, icon("chevron"), "Advanced"),
          h("div", { class: "stack" },
            secretField("RIGHT_API_KEY", "API key override"),
            h("div", { class: "form-grid" },
              textField("RIGHT_TIMEOUT_S", { type: "number" }),
              textField("RIGHT_TEMPERATURE", { type: "number", placeholder: "unset" })),
            boolField("RIGHT_JSON_MODE"))))));
}

function leftSection() {
  const data = settings.data;
  const selectedName = current("LEFT_PROVIDER") || fieldInfo("LEFT_PROVIDER").default || "auto";
  const provider = data.left_providers.find((p) => p.name === selectedName) || { name: selectedName, settings: [] };
  const blocks = [];
  if (provider.experimental) {
    blocks.push(h("div", { class: "notice warn" }, icon("alert"), h("div", { class: "grow" },
      h("strong", {}, "Experimental. "),
      "A chat model read through its token probabilities. They are not calibrated, they tend to sit near 0 or 1, and the harness's thresholds assume calibration. Use it only if you cannot run a decision model.")));
  }
  if (provider.name === "mock") {
    blocks.push(h("div", { class: "notice info" }, icon("info"), h("div", { class: "grow" },
      "An offline stand-in. It answers every policy question 0.5 and picks options by word overlap. Its numbers mean nothing.")));
  }
  if (provider.name === "auto") {
    blocks.push(h("p", { class: "muted" }, "Uses Jev when its API key is set, and the mock otherwise."), secretField("JEV_API_KEY", "Jev API key"));
  }
  for (const name of provider.settings || []) {
    const f = fieldInfo(name);
    if (!f) continue;
    if (name === "LEFT_MODEL" || name === "JEV_MODEL") blocks.push(modelField("left", provider));
    else if (f.kind === "secret") blocks.push(secretField(name));
    else if (name === "LEFT_BASE_URL") {
      blocks.push(textField(name, { placeholder: provider.base_url || "http://your-server:port (required)" }));
    } else blocks.push(textField(name, { type: f.kind === "number" ? "number" : "text" }));
  }
  if (provider.plugin) blocks.push(h("p", { class: "muted" }, "A plugin provider. Configure it through the variables its documentation names."));
  return h("div", {},
    h("div", { class: "settings-title" }, "Left hemisphere · the Emissary"),
    h("p", { class: "settings-sub" }, "A decision model that answers typed questions with calibrated probabilities. It commits only routine, low-stakes picks it is sure of, and checks every option against your policy."),
    providerTiles("left", data.left_providers, provider.name),
    h("div", { class: "provider-card" },
      h("div", { class: "head" }, h("span", { class: "n" }, providerName(provider.name, "left")), h("code", { class: "faint" }, provider.name)),
      h("div", { class: "stack" }, blocks)));
}

function loopSection() {
  const num = (name) => {
    const v = current(name);
    const f = fieldInfo(name);
    return Number(v === null || v === undefined || v === "" ? f.default : v);
  };
  const no = num("HARNESS_PERMIT_NO_THRESHOLD");
  const yes = num("HARNESS_PERMIT_YES_THRESHOLD");
  const bands = h("div", { class: "bands" });
  if (no >= 0 && yes <= 1 && no < yes) {
    for (const [cls, width] of [["refused", no], ["unsure", yes - no], ["allowed", 1 - yes]]) {
      const part = h("div", { class: cls });
      part.style.width = pct(width);
      bands.append(part);
    }
  }
  return h("div", {},
    h("div", { class: "settings-title" }, "Loop and policy"),
    h("p", { class: "settings-sub" }, "When the left may commit on its own, and the rules that bind both hemispheres."),
    h("div", { class: "stack" },
      textField("HARNESS_POLICY", { rows: 3, placeholder: "for example: Refunds or credits above $500 need human sign-off." }),
      h("div", { class: "card" },
        h("div", { class: "field-label" }, "Policy gate"),
        bands,
        h("div", { class: "bands-legend" }, h("span", {}, `refused ≤ ${fmt(no)}`), h("span", {}, "unsure: the right decides"), h("span", {}, `allowed ≥ ${fmt(yes)}`)),
        h("div", { class: "form-grid" },
          textField("HARNESS_PERMIT_NO_THRESHOLD", { type: "number" }),
          textField("HARNESS_PERMIT_YES_THRESHOLD", { type: "number" }))),
      h("div", { class: "form-grid" },
        textField("HARNESS_COMMIT_THRESHOLD", { type: "number" }),
        textField("HARNESS_MAX_STEPS", { type: "number" }))));
}

function aboutSection() {
  const s = state.status || {};
  return h("div", {},
    h("div", { class: "settings-title" }, "About"),
    h("p", { class: "settings-sub" }, "An agent harness after McGilchrist's The Master and His Emissary. Experimental."),
    h("dl", { class: "kv" },
      h("dt", {}, "Version"), h("dd", {}, s.version || ""),
      h("dt", {}, "Settings file"), h("dd", {}, h("code", {}, settings.data.env_file)),
      h("dt", {}, "Keys"), h("dd", {}, "Written to the settings file, which only your user can read. The page never receives a saved key."),
      h("dt", {}, "Precedence"), h("dd", {}, "A variable exported in the shell that started the server overrides the file. Such fields are marked “from your shell”."),
      h("dt", {}, "Actions"), h("dd", {}, "Always simulated in the web UI.")));
}

// ---------------------------------------------------------------------------------------
// Routing and start-up

function route() {
  const run = location.hash.match(/^#\/run\/([\w-]+)$/);
  const section = location.hash.match(/^#\/settings(?:\/(\w+))?$/);
  if (run) {
    showRun(run[1]);
    return;
  }
  if (!section || state.view !== "composer") showComposer();
  if (section) openSettings(section[1] || "right");
}

async function boot() {
  document.getElementById("menu").append(icon("menu"));
  document.getElementById("new-run").prepend(icon("plus"));
  document.getElementById("open-settings").prepend(icon("settings"));
  document.getElementById("new-run").addEventListener("click", () => {
    closeSidebar();
    if (location.hash === "#/new" || location.hash === "" || location.hash === "#/") showComposer();
    else location.hash = "#/new";
  });
  document.getElementById("open-settings").addEventListener("click", () => { closeSidebar(); openSettings(); });
  document.getElementById("menu").addEventListener("click", () => document.getElementById("sidebar").classList.toggle("open"));
  document.getElementById("settings").addEventListener("click", (event) => { if (event.target.id === "settings") closeSettings(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !document.getElementById("settings").hidden) closeSettings();
  });
  window.addEventListener("hashchange", route);

  await Promise.all([refreshStatus(), refreshRuns()]);
  route();
  setInterval(() => {
    if (state.runs.some((run) => run.status === "running")) refreshRuns();
  }, 4000);
}

boot();

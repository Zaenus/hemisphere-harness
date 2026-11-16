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

function append(el, ...children) {
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
  anthropic: "Claude · Anthropic",
  gemini: "Gemini · Google",
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

// Runs grouped into conversations, most recent activity first.
function conversations() {
  const groups = new Map();
  for (const run of state.runs) {
    const id = run.conversation_id || run.id;
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(run);
  }
  return [...groups.entries()]
    .map(([id, runs]) => {
      runs.sort((a, b) => a.created.localeCompare(b.created));
      return { id, first: runs[0], last: runs[runs.length - 1], count: runs.length };
    })
    .sort((a, b) => b.last.created.localeCompare(a.last.created));
}

function renderRunList() {
  const list = clear(document.getElementById("run-list"));
  if (!state.runs.length) {
    list.append(h("div", { class: "empty" }, "No conversations yet."));
    return;
  }
  for (const c of conversations()) {
    list.append(
      h("button", {
        class: `run-item ${c.id === state.conversationId ? "active" : ""}`,
        type: "button",
        onclick: () => { location.hash = `#/c/${c.id}`; closeSidebar(); },
      },
        h("span", { class: `dot ${c.last.status}` }),
        h("span", {},
          h("div", { class: "goal" }, c.first.message || c.first.goal),
          h("div", { class: "when" }, relativeTime(c.last.created),
            c.count > 1 ? ` · ${c.count} messages` : "", c.first.demo ? " · demo" : ""))),
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
  state.conversationId = null;
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
      location.hash = `#/c/${run.conversation_id}`;
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
  if (s.tools_config) {
    notices.push(h("div", { class: "notice info" }, icon("bolt"),
      h("div", { class: "grow" }, h("strong", {}, "Tools are connected. "),
        "Live runs can call the tools in ", h("code", {}, s.tools_config),
        ". Those calls are real. Tools that change things wait for your sign-off."),
      h("button", { class: "btn small", type: "button", onclick: () => openSettings("tools") }, "See tools")));
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
      h("div", { class: "section-title" }, "Or start from an example"),
      h("div", { class: "examples" }, EXAMPLES.map((example) =>
        h("button", { class: "btn small", type: "button", title: example.goal, onclick: () => showComposer({ goal: example.goal, context: example.context }) },
          example.label))),
      h("div", { class: "section-title" }, "Or try a demo ticket"),
      h("div", { class: "demos" },
        demo("lockout", "Locked-out customer", "Routine. The left commits a step on its own, then hands a near-tie back to the right."),
        demo("refund", "$2,400 refund", "High stakes. The left only advises, and the policy requires a human to sign off.")),
    ),
  );
  if (rightReady) goal.focus();
}

// What the run cost, from its end event: tokens and money for the right, time for both.
function usageChip(run) {
  const end = run.events.find((event) => event.phase === "end");
  const u = end && end.data && end.data.usage;
  if (!u) return null;
  const tokens = u.right.input_tokens + u.right.output_tokens;
  const k = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  const money = u.cost == null ? null : u.cost < 0.01 ? `$${u.cost.toFixed(4)}` : `$${u.cost.toFixed(2)}`;
  const label = [tokens ? `${k(tokens)} tokens` : null, money, `${Math.round(u.seconds)} s`].filter(Boolean).join(" · ");
  const title = [
    `Right: ${u.right.calls} call(s), ${k(u.right.input_tokens)} in, ${k(u.right.output_tokens)} out, ${u.right.seconds.toFixed(1)} s waiting`,
    `Left: ${u.left.calls} call(s), ${u.left.seconds.toFixed(1)} s waiting`,
    u.cost == null ? "Set RIGHT_PRICE_INPUT and RIGHT_PRICE_OUTPUT (dollars per million tokens) to see the cost." : null,
  ].filter(Boolean).join("\n");
  return h("span", { class: "chip", title }, label);
}

// Starting points beyond support tickets. They fill the composer; nothing runs until you start it.
const EXAMPLES = [
  { label: "A research question", goal: "Is it true that Bitcoin is a decentralized currency?", context: "" },
  {
    label: "Review some code",
    goal: "Review this function for bugs and suggest a corrected version.",
    context: "def average_order_value(orders, discounts=[]):\n    total = 0\n    for order in orders:\n        total += order['amount']\n        discounts.append(order.get('discount', 0))\n    return (total - sum(discounts)) / len(orders)",
  },
  {
    label: "Plan a migration",
    goal: "Plan how to move our app's database from SQLite to PostgreSQL with as little downtime as possible.",
    context: "Django app, about 12 GB of data, roughly 200 requests a minute at peak, one production server. We can schedule a short maintenance window at night.",
  },
  {
    label: "Write a reply",
    goal: "Draft a reply to a customer who received the wrong item.",
    context: "Order #7731: they ordered a blue backpack and received a green one. They need it for a trip in five days. We have the blue one in stock and offer free express shipping for our mistakes.",
  },
];

async function startDemo(key) {
  try {
    const run = await api("/api/runs", { method: "POST", body: { demo: key } });
    location.hash = `#/c/${run.conversation_id}`;
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
  draft: "Drafts the answer",
  check: "Checks the claims",
  revise: "Revises the answer",
  tools: "Connects tools",
  recall: "Recalls",
  remember: "Remembers",
  lookup: "Looks claims up",
  screen: "Reads the tool's output first",
  style: "Checks the answer's form",
  approve: "Sign off",
  reject: "Reject",
  report: "Report",
  end: "End",
};
const WHO = { right: "Right · Master", left: "Left · Emissary", harness: "Harness", human: "You" };
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
  // Older links point at a single run: open its conversation instead.
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    location.replace(`#/c/${run.conversation_id}`);
  } catch (error) {
    if (error.status === 404) {
      toast("That run no longer exists.", "error");
      location.hash = "#/";
    }
  }
}

async function showConversation(id) {
  closeStream();
  state.view = "conversation";
  let conversation;
  try {
    conversation = await api(`/api/conversations/${encodeURIComponent(id)}`);
  } catch (error) {
    if (error.status === 404) {
      toast("That conversation no longer exists.", "error");
      location.hash = "#/";
    }
    return;
  }
  state.conversationId = conversation.id;
  renderRunList();
  const view = new ConversationView(conversation);
  clear(document.getElementById("main")).append(view.root);
  view.listenIfRunning();
}

// A conversation: one turn per run, then a box to reply. Each reply is a new run that
// remembers the conversation; a sign-off continues the run that stopped for it.
class ConversationView {
  constructor(conversation) {
    this.id = conversation.id;
    this.runs = conversation.runs;
    this.draft = "";
    this.turns = h("div", { class: "turns" });
    this.replyBox = h("div", { class: "reply" });
    this.root = h("div", { class: "page chat" }, this.turns, this.replyBox);
    this.views = this.runs.map((run) => this.addTurn(run));
    this.renderReply();
  }

  addTurn(run) {
    const view = new RunView(run, this);
    this.turns.append(view.root);
    return view;
  }

  isFirst(run) {
    return this.runs[0].id === run.id;
  }

  isLast(run) {
    return this.runs[this.runs.length - 1].id === run.id;
  }

  running() {
    return this.runs.some((run) => run.status === "running");
  }

  listenIfRunning() {
    const view = this.views.find((v) => v.run.status === "running");
    if (view) view.listen();
    const main = document.getElementById("main");
    main.scrollTop = main.scrollHeight;
  }

  async reply(text) {
    const run = await api("/api/runs", { method: "POST", body: { goal: text, conversation_id: this.id } });
    this.draft = "";
    this.begin(run);
  }

  async resume(run, decision) {
    const next = await api(`/api/runs/${encodeURIComponent(run.id)}/resume`, { method: "POST", body: decision });
    this.begin(next);
  }

  begin(run) {
    this.runs.push(run);
    const view = this.addTurn(run);
    this.views.push(view);
    for (const other of this.views) if (other !== view) other.refresh(); // the old sign-off panel goes
    this.renderReply();
    view.listen();
    view.root.scrollIntoView({ behavior: "smooth", block: "start" });
    refreshRuns();
  }

  ended() {
    this.renderReply();
    refreshRuns();
  }

  async remove() {
    if (!confirm("Delete this whole conversation from the history?")) return;
    try {
      await api(`/api/conversations/${encodeURIComponent(this.id)}`, { method: "DELETE" });
      location.hash = "#/";
      refreshRuns();
    } catch (error) {
      if (error.status !== 401) toast(error.message, "error");
    }
  }

  renderReply() {
    const s = state.status || { right: {} };
    const busy = this.running();
    const ready = !!s.right.ready;
    const last = this.runs[this.runs.length - 1];
    const box = h("textarea", {
      class: "input", rows: 2, value: this.draft, "aria-label": "Reply",
      placeholder: ready ? "Reply…" : "Replies need a right hemisphere. Set one up in Settings.",
      disabled: !ready,
    });
    const send = h("button", { class: "btn primary", type: "button", disabled: busy || !ready || !this.draft.trim() }, icon("play"), "Send");
    const submit = async () => {
      const text = box.value.trim();
      if (!text || busy || !ready) return;
      send.disabled = true;
      try {
        await this.reply(text);
      } catch (error) {
        if (error.status !== 401) toast(error.message, "error");
        send.disabled = false;
      }
    };
    box.addEventListener("input", () => {
      this.draft = box.value;
      send.disabled = busy || !ready || !box.value.trim();
    });
    box.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        submit();
      }
    });
    send.addEventListener("click", submit);
    const hint = busy
      ? "The agent is working. You can write your reply meanwhile."
      : last.status === "needs_human"
        ? "Or approve or reject above. A reply starts a new run, which may stop for the same sign-off."
        : "Each reply is a new run that remembers this conversation. Enter sends, Shift+Enter adds a line.";
    clear(this.replyBox).append(
      h("div", { class: "card reply-card" }, box,
        h("div", { class: "foot" }, h("span", { class: "faint" }, hint), send)));
  }
}

const STEPS_PREFERENCE = "hh.showSteps";

function stepsOpenPreference() {
  try {
    return localStorage.getItem(STEPS_PREFERENCE) === "1";
  } catch {
    return false;
  }
}

function rememberStepsOpen(open) {
  try {
    localStorage.setItem(STEPS_PREFERENCE, open ? "1" : "0");
  } catch {
    // storage unavailable: the toggle still works for this page
  }
}

// A run reads like a chat: your request, the harness's work folded into one line, then
// the right hemisphere's answer.
class RunView {
  constructor(run, conversation) {
    this.run = run;
    this.conversation = conversation;
    this.steps = new Map();
    this.open = false;
    this.timeline = h("div", { class: "timeline" });
    this.workDot = h("span", { class: "dot", hidden: true });
    this.workLine = h("span", { class: "work-line" });
    this.workCount = h("span", { class: "work-count" });
    this.toggle = h("button", { class: "work-toggle", type: "button", "aria-expanded": "false", onclick: () => this.setOpen(!this.open) },
      icon("chevron"), this.workDot, this.workLine, this.workCount);
    this.workBody = h("div", { class: "work-body", hidden: true }, this.timeline);
    this.work = h("section", { class: "work", "aria-label": "How the harness worked" }, this.toggle, this.workBody);
    this.net = h("div", { class: "network" });
    this.stepHeads = new Map();
    this.answer = h("div", { class: "answer" });
    this.foot = h("div", { class: "answer-foot" });
    this.after = h("div", { class: "stack" });
    this.root = h("div", { class: "turn" },
      this.userMessage(),
      h("div", { class: "msg-assistant" },
        h("div", { class: "assistant-head" }, h("img", { src: "/favicon.svg", alt: "", width: 22, height: 22 }), "Hemisphere Harness"),
        this.net, this.work, this.answer, this.foot, this.after));
    for (const event of run.events) this.add(event);
    this.setOpen(stepsOpenPreference(), false);
    this.refresh();
  }

  userMessage() {
    const run = this.run;
    // Context and policy belong to the conversation: show them with its first message only.
    const first = this.conversation.isFirst(run);
    const extras = first ? [run.context ? "context" : null, run.policy ? "policy" : null].filter(Boolean) : [];
    return h("div", { class: `msg-user${run.resumed_from ? " signoff-msg" : ""}` },
      h("div", { class: "bubble" },
        run.resumed_from ? h("div", { class: "bubble-tag" }, "Your sign-off") : null,
        h("div", { class: "bubble-text" }, run.message || run.goal),
        extras.length
          ? h("details", { class: "more small" },
            h("summary", {}, icon("chevron"), extras.join(" and ")),
            run.context ? h("div", { class: "context-box" }, run.context) : null,
            run.policy ? h("div", { class: "context-box" }, h("strong", {}, "Policy: "), run.policy) : null)
          : null));
  }

  setOpen(open, remember = true) {
    this.open = open;
    this.workBody.hidden = !open;
    this.work.classList.toggle("open", open);
    this.toggle.setAttribute("aria-expanded", String(open));
    if (remember) rememberStepsOpen(open);
    this.updateCount();
  }

  updateCount() {
    const steps = new Set(this.run.events.filter((e) => e.step !== null && e.step !== undefined).map((e) => e.step)).size;
    this.workCount.textContent = this.open ? "Hide steps" : steps ? `Show ${steps} step${steps === 1 ? "" : "s"}` : "Show details";
  }

  renderNetwork() {
    const element = renderNetwork(this.run, { onSelect: (step) => this.showStep(step), available: (this.net.clientWidth || 832) - 24 }); // slack for a page scrollbar appearing later
    const atEnd = this.net.scrollLeft + this.net.clientWidth >= this.net.scrollWidth - 4;
    clear(this.net).append(element);
    if (this.run.status === "running" && atEnd) this.net.scrollLeft = this.net.scrollWidth;
  }

  showStep(step) {
    this.setOpen(true);
    const head = this.stepHeads.get(step);
    if (head) head.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  refresh() {
    const run = this.run;
    const running = run.status === "running";
    this.renderNetwork();

    if (running) {
      const [actor, text] = nextActivity(run);
      this.workDot.className = `dot ${actor} live`;
      this.workDot.hidden = false;
      this.workLine.textContent = text;
    } else {
      this.workDot.hidden = true;
      this.workLine.textContent = workSummary(run);
      this.workLine.title = this.workLine.textContent;
    }
    this.updateCount();

    const reportEvent = run.events.find((event) => event.phase === "report");
    const report = reportEvent ? (reportEvent.data.report ?? reportEvent.summary) : run.report;
    clear(this.answer);
    if (report) this.answer.append(safeMarkdown(report));
    else if (running && this.liveText) this.answer.append(...this.liveAnswer());
    else if (running) this.answer.append(h("div", { class: "typing", "aria-label": "Working" }, h("span"), h("span"), h("span")));
    else if (run.status === "cancelled") this.answer.append(h("p", { class: "muted" }, "Stopped before the right hemisphere wrote its answer."));
    if (run.error && run.status === "failed") {
      this.answer.append(h("div", { class: "notice danger" }, icon("alert"),
        h("div", { class: "grow" }, h("strong", {}, "The run failed."), h("div", { class: "output" }, run.error))));
    }

    const last = this.conversation.isLast(run);
    const first = this.conversation.isFirst(run);
    const signedOff = run.status === "needs_human" && this.conversation.runs.some((r) => r.resumed_from === run.id);
    append(clear(this.foot), [
      signedOff
        ? h("span", { class: "chip status-done" }, "Signed off")
        : h("span", { class: `chip status-${run.status}` }, STATUS[run.status] || run.status),
      h("span", { class: "chip right", title: "Right hemisphere" }, run.right),
      h("span", { class: "chip left", title: "Left hemisphere" }, run.left),
      run.left_experimental ? h("span", { class: "chip warn" }, "experimental left") : null,
      usageChip(run),
      h("span", { class: "spacer" }),
      report && !running ? h("button", { class: "btn small ghost", type: "button", onclick: () => this.copy(report) }, "Copy") : null,
      running ? h("button", { class: "btn small danger", type: "button", onclick: () => this.stop() }, icon("stop"), "Stop") : null,
      !running && first ? h("button", { class: "btn small ghost", type: "button", title: "Start a new conversation with the same request", onclick: () => this.again() }, icon("again"), "Run again") : null,
      !running && last ? h("button", { class: "btn small ghost", type: "button", title: "Delete the whole conversation", onclick: () => this.conversation.remove() }, icon("trash"), "Delete") : null]);

    clear(this.after);
    if (run.status === "needs_human" && last && run.can_resume) {
      this.after.append(h("div", { class: "end-banner needs_human" }, icon("alert"),
        h("div", { class: "grow" }, h("h3", {}, "Waiting for your sign-off"), signOff(run, this.conversation))));
    } else if (run.status === "needs_human" && last) {
      this.after.append(h("div", { class: "notice info" }, icon("info"),
        h("div", { class: "grow" }, "This run was saved before sign-offs could continue a run. Reply to carry on.")));
    } else if (run.status === "max_steps" && last) {
      this.after.append(h("div", { class: "notice info" }, icon("info"),
        h("div", { class: "grow" }, `Stopped at the step limit of ${run.config.max_steps}. Reply to let it carry on.`)));
    }
  }

  listen() {
    const after = this.run.events.length - 1;
    const source = new EventSource(`/api/runs/${encodeURIComponent(this.run.id)}/events?after=${after}`);
    state.source = source;
    source.addEventListener("trace", (message) => {
      const main = document.getElementById("main");
      const atBottom = main.scrollHeight - main.scrollTop - main.clientHeight < 80;
      const event = JSON.parse(message.data);
      this.run.events.push(event);
      this.add(event);
      this.refresh();
      if (atBottom) main.scrollTop = main.scrollHeight;
    });
    source.addEventListener("delta", (message) => {
      // A piece of the answer as it is written. The final answer replaces it.
      const piece = JSON.parse(message.data);
      this.liveText = piece.reset ? "" : (this.liveText || "") + (piece.text || "");
      if (!this.liveFrame) {
        this.liveFrame = requestAnimationFrame(() => {
          this.liveFrame = null;
          if (this.run.status !== "running" || this.run.events.some((e) => e.phase === "report")) return;
          const main = document.getElementById("main");
          const atBottom = main.scrollHeight - main.scrollTop - main.clientHeight < 80;
          clear(this.answer).append(...this.liveAnswer());
          if (atBottom) main.scrollTop = main.scrollHeight;
        });
      }
    });
    source.addEventListener("end", (message) => {
      Object.assign(this.run, JSON.parse(message.data));
      notifyBrowser(this.run);
      source.close();
      if (state.source === source) state.source = null;
      this.refresh();
      this.conversation.ended();
    });
    source.addEventListener("error", () => {
      if (source.readyState === EventSource.CLOSED) {
        this.workDot.hidden = true;
        this.workLine.textContent = "Lost the connection to the server. Reload to reconnect.";
      }
    });
  }

  liveAnswer() {
    const checking = this.run.events.some((e) => e.phase === "draft");
    return [
      safeMarkdown(this.liveText || ""),
      h("div", { class: "live-note" }, h("span", { class: "dot right live" }),
        checking ? "Checking this answer before it is final…" : "Writing…"),
    ];
  }

  async copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied");
    } catch {
      toast("Could not copy: the browser refused.", "error");
    }
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

  container(step) {
    if (step === null || step === undefined) return this.timeline;
    if (!this.steps.has(step)) {
      const group = h("div", { class: "timeline" });
      const head = h("div", { class: "step-head" }, `Step ${step}`);
      this.timeline.append(head, group);
      this.steps.set(step, group);
      this.stepHeads.set(step, head);
    }
    return this.steps.get(step);
  }

  add(event) {
    if (event.phase === "end" || event.phase === "report") return; // the report is the answer
    let card;
    try {
      card = renderEvent(event, this.run);
    } catch (error) {
      console.error("could not draw a step", event, error);
      card = h("div", { class: `event ${event.actor}` }, h("div", { class: "text" }, event.summary));
    }
    this.container(event.step).append(card);
  }
}

// One line for a finished run: how many steps, and who settled them.
function workSummary(run) {
  const steps = new Set(run.events.filter((e) => e.step !== null && e.step !== undefined).map((e) => e.step)).size;
  const final = new Map();
  for (const event of run.events) {
    if ((event.phase === "gate" || event.phase === "approve" || event.phase === "reject") && event.data.decision) {
      final.set(event.step, { ...event.data.decision, byYou: event.actor === "human" });
    }
  }
  const count = { left: 0, right: 0, revise: 0, human: 0, approved: 0, rejected: 0 };
  for (const decision of final.values()) {
    if (decision.byYou) count[decision.outcome === "proceed" ? "approved" : "rejected"] += 1;
    else if (decision.outcome === "proceed") count[decision.committed_by === "left" ? "left" : "right"] += 1;
    else if (decision.outcome === "revise") count.revise += 1;
    else if (decision.outcome === "flag_human") count.human += 1;
  }
  const parts = [];
  if (count.approved) parts.push(`you approved ${count.approved}`);
  if (count.rejected) parts.push(`you rejected ${count.rejected}`);
  const check = run.events.find((e) => e.phase === "check" && e.data.checks);
  const revised = run.events.some((e) => e.phase === "revise" && e.data.revised);
  if (count.left) parts.push(`the left committed ${count.left}`);
  if (count.right) parts.push(`the right decided ${count.right}`);
  if (count.revise) parts.push(`${count.revise} revised`);
  if (count.human) parts.push(`${count.human} waiting for you`);
  if (!steps) return run.events.length ? "Planned, no steps taken" : "Did not start";
  let line = `${steps} step${steps === 1 ? "" : "s"}${parts.length ? ": " + parts.join(", ") : ""}`;
  if (check) {
    const n = check.data.checks.length;
    const held = check.data.checks.filter((c) => c.verdict === "holds").length;
    const unsure = check.data.checks.filter((c) => c.verdict === "unsure").length;
    const verdicts = [`${held} held`, unsure ? `${unsure} unsure` : null, n - held - unsure ? `${n - held - unsure} doubtful` : null].filter(Boolean);
    line += ` · ${n} claim${n === 1 ? "" : "s"} checked: ${verdicts.join(", ")}${revised ? ", answer revised" : ""}`;
  }
  return line;
}

// Markdown that cannot take the page down: if formatting fails, show the plain text.
function safeMarkdown(text) {
  try {
    return renderMarkdown(text);
  } catch (error) {
    console.error("could not format an answer", error);
    return h("div", { class: "md" }, h("p", { class: "text" }, text));
  }
}

// Markdown for the answer, built as DOM nodes. Never parsed as HTML.
function renderMarkdown(text) {
  const root = h("div", { class: "md" });
  const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
  let paragraph = [];
  let list = null;

  const flush = () => {
    if (paragraph.length) {
      const p = h("p");
      paragraph.forEach((line, index) => {
        if (index) p.append(h("br"));
        append(p, inline(line));
      });
      root.append(p);
      paragraph = [];
    }
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      list = null;
      const code = [];
      for (i += 1; i < lines.length && !/^\s*```/.test(lines[i]); i += 1) code.push(lines[i]);
      root.append(h("pre", {}, h("code", {}, code.join("\n"))));
      continue;
    }
    if (!line.trim()) {
      flush();
      list = null;
      continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/) || line.match(/^\s*()\*\*([^*]+)\*\*:?\s*$/);
    if (heading) {
      flush();
      list = null;
      root.append(h(heading[1].length && heading[1].length <= 2 ? "h3" : "h4", {}, inline(heading[2])));
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      list = null;
      root.append(h("hr"));
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      flush();
      list = null;
      const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
      const head = h("tr", {}, cells(line).map((c) => h("th", {}, inline(c))));
      const body = h("tbody");
      for (i += 2; i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]); i += 1) {
        body.append(h("tr", {}, cells(lines[i]).map((c) => h("td", {}, inline(c)))));
      }
      i -= 1;
      root.append(h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, head), body)));
      continue;
    }
    const bullet = line.match(/^\s*[-*•+]\s+(.*)$/);
    const ordered = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    if (bullet || ordered) {
      flush();
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = h(tag, ordered ? { start: ordered[1] } : {});
        root.append(list);
      }
      list.append(h("li", {}, inline(bullet ? bullet[1] : ordered[2])));
      continue;
    }
    if (list && /^\s{2,}\S/.test(line)) {
      append(list.lastChild, " ", inline(line.trim()));
      continue;
    }
    list = null;
    paragraph.push(line.trim());
  }
  flush();
  return root;
}

function inline(text) {
  const out = [];
  // Bold, inline code, *italic*, and http(s) links. Single underscores are left alone,
  // because option ids such as process_refund are full of them.
  const pattern = /(\*\*[^*\n]+?\*\*|`[^`\n]+`|\*[^*\s][^*\n]*?\*|\[[^\]\n]+\]\((https?:\/\/[^\s)]+)\))/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**")) out.push(h("strong", {}, inline(token.slice(2, -2))));
    else if (token.startsWith("`")) out.push(h("code", {}, token.slice(1, -1)));
    else if (token.startsWith("[")) out.push(h("a", { href: match[2], target: "_blank", rel: "noopener noreferrer" }, token.slice(1, token.indexOf("]("))));
    else out.push(h("em", {}, inline(token.slice(1, -1))));
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function nextActivity(run) {
  const events = run.events;
  const last = events[events.length - 1];
  if (!last) return run.resumed_from ? ["harness", "Continuing with your decision…"] : ["right", "The right hemisphere is planning…"];
  const step = last.step || 0;
  switch (last.phase) {
    case "recall":
      return ["right", "The right hemisphere is planning…"];
    case "tools":
      return ["right", "The right hemisphere is planning…"];
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
      return last.data.action && last.data.action.tool
        ? ["left", "The left is reading what the tool returned…"]
        : ["right", `The right is reintegrating step ${step}…`];
    case "screen":
      return ["right", `The right is reintegrating step ${step}…`];
    case "reintegrate": {
      const gate = [...events].reverse().find((e) => e.phase === "gate" && e.step === step);
      const flagged = gate && gate.data.decision && gate.data.decision.outcome === "flag_human";
      const met = last.data.reintegration && last.data.reintegration.goal_met;
      if (flagged || met || step >= run.config.max_steps) return ["right", "The right is writing the answer…"];
      return ["right", `The right is proposing options for step ${step + 1}…`];
    }
    case "approve":
      return ["harness", "Carrying out what you approved…"];
    case "reject":
      return ["right", "The right is taking your rejection into account…"];
    case "draft": {
      const n = (last.data.claims || []).length;
      if (n) return ["left", `The left is checking ${n} claim${n === 1 ? "" : "s"} in the answer…`];
      return (last.data.preferences || []).length ? ["left", "The left is checking the answer against your preferences…"] : ["harness", "Finishing…"];
    }
    case "lookup":
      return ["left", "The left is judging those claims again, with what was found…"];
    case "check": {
      const weak = (last.data.checks || []).filter((c) => c.verdict !== "holds").length;
      return weak ? ["right", `The right is revising ${weak} claim${weak === 1 ? "" : "s"} that did not hold…`] : ["harness", "Finishing…"];
    }
    case "style": {
      const missed = (last.data.preferences || []).filter((c) => !c.followed).length;
      return missed ? ["right", "The right is rewriting the answer the way you like it…"] : ["harness", "Finishing…"];
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
          h("div", { class: "option" }, h("code", {}, o.id), h("span", { class: "desc" }, o.description, toolCall(o))))));
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
      const option = findOption(run, event.step, a.option_id);
      return card(a.tool ? null : "simulated",
        h("div", { class: "chips" }, h("code", {}, a.option_id),
          a.tool ? h("span", { class: "chip harness", title: "This really ran." }, `tool: ${a.tool}`) : null,
          a.ok ? null : h("span", { class: "chip danger" }, "failed")),
        option && option.tool ? h("div", { class: "sub mono" }, JSON.stringify(option.arguments || {})) : null,
        h("div", { class: "output" }, a.output));
    }
    case "tools": {
      const tools = d.tools || [];
      const errors = Object.entries(d.errors || {});
      const problems = Object.entries(d.problems || {});
      return card(null,
        h("div", { class: "text" }, `${tools.length} tool${tools.length === 1 ? "" : "s"} available to this run.`),
        tools.length ? h("div", { class: "chips spaced" }, tools.map((t) =>
          h("span", { class: `chip ${t.needs_approval ? "warn" : ""}`, title: t.description }, t.name))) : null,
        errors.map(([server, error]) => h("div", { class: "notice warn spaced" }, icon("alert"),
          h("div", { class: "grow" }, h("strong", {}, `${server} did not start. `), error))),
        problems.map(([name, why]) => h("div", { class: "notice warn spaced" }, icon("alert"),
          h("div", { class: "grow" }, h("strong", {}, `${name} is not available. `), why))));
    }
    case "reintegrate": {
      const r = d.reintegration;
      if (!r) return fallback(event, card);
      return card(null,
        h("div", { class: "text" }, r.situation),
        h("div", { class: "chips spaced" }, h("span", { class: `chip ${r.goal_met ? "ok" : ""}` }, r.goal_met ? "goal met" : "goal not yet met")));
    }
    case "approve": {
      const option = d.option || {};
      return card(null,
        h("span", { class: "verdict proceed" }, icon("check"), `You approved '${option.id || (d.decision && d.decision.option_id)}'`),
        option.description ? h("div", { class: "sub" }, option.description) : null,
        event.details.map((line) => h("div", { class: "sub" }, `Your note: ${line}`)));
    }
    case "reject":
      return card(null,
        h("span", { class: "verdict revise" }, icon("x"), "You rejected these options"),
        event.details.map((line) => h("div", { class: "sub" }, `Your note: ${line}`)));
    case "draft": {
      if (!d.draft) return fallback(event, card);
      const claims = d.claims || [];
      return card(null,
        h("div", { class: "sub" }, `Drafted the answer and listed ${claims.length} factual claim${claims.length === 1 ? "" : "s"} for the left to check.`),
        h("details", { class: "more" }, h("summary", {}, icon("chevron"), "The draft, before the check"),
          h("div", { class: "draft-box" }, safeMarkdown(d.draft))));
    }
    case "check": {
      if (d.error !== undefined) {
        return card(null, h("div", { class: "notice warn" }, icon("alert"),
          h("div", { class: "grow" }, h("strong", {}, "The check could not run, so the draft stands. "), h("div", { class: "output" }, d.error))));
      }
      if (!d.checks) return fallback(event, card);
      const t = d.thresholds || { holds: run.config.permit_yes_threshold, doubtful: run.config.permit_no_threshold };
      const meta = `${d.model}${d.latency_ms != null ? ` · ${Math.round(d.latency_ms)} ms` : ""}`;
      return card(meta,
        h("div", { class: "bars-title" }, "Does each claim hold? ", h("span", { class: "faint" }, `(doubtful ≤ ${fmt(t.doubtful)} · holds ≥ ${fmt(t.holds)})`)),
        h("div", { class: "claims" }, d.checks.map((c) => {
          const fill = h("div", { class: `bar-fill ${c.verdict === "holds" ? "allowed" : c.verdict === "doubtful" ? "refused" : "unsure"}` });
          fill.style.width = pct(c.probability);
          const track = h("div", { class: "bar-track" }, fill);
          for (const m of [t.doubtful, t.holds]) {
            const marker = h("div", { class: "marker" });
            marker.style.left = `calc(${pct(m)} - 1px)`;
            track.append(marker);
          }
          const sources = c.sources || [];
          return h("div", { class: "claim-row" },
            h("div", { class: "claim-text" }, c.claim.text),
            c.first_probability != null ? h("div", { class: "faint small-note" }, `before the lookup: ${fmt(c.first_probability)}`) : null,
            sources.length ? h("div", { class: "sources" }, "Sources: ", sources.map((s, i) => [i ? ", " : "",
              /^https?:\/\//.test(s.url) ? h("a", { href: s.url, target: "_blank", rel: "noopener noreferrer" }, s.title) : s.title])) : null,
            h("div", { class: "claim-bar" }, track, h("span", { class: "val" }, fmt(c.probability)),
              h("span", { class: `chip ${c.verdict === "holds" ? "ok" : c.verdict === "doubtful" ? "danger" : ""}` }, c.verdict)));
        })));
    }
    case "recall": {
      const recalled = d.recalled || [];
      const prefs = d.preferences || 0;
      const facts = recalled.length - prefs;
      return card(null,
        h("div", { class: "text" }, recalled.length
          ? `Recalled ${prefs ? `${prefs} preference${prefs === 1 ? "" : "s"}` : ""}${prefs && d.considered ? " and " : ""}${d.considered ? `${facts} of ${d.considered} fact${d.considered === 1 ? "" : "s"}` : ""} from earlier conversations.`
          : `None of the ${d.considered} saved fact${d.considered === 1 ? "" : "s"} bore on this.`),
        recalled.length ? h("ul", {}, recalled.map((n) => h("li", {}, n.text))) : null);
    }
    case "remember": {
      const notes = d.notes || [];
      return card(null, h("div", { class: "text" }, "Kept for later conversations:"), h("ul", {}, notes.map((n) => h("li", {}, n.text))));
    }
    case "lookup": {
      const results = d.results || [];
      return card(d.source || null,
        h("div", { class: "text" }, `Looked up ${results.length} claim${results.length === 1 ? "" : "s"} the left was unsure of.`),
        results.map((r) => h("div", { class: "sub" }, r.claim.text, " → ",
          r.sources.length ? r.sources.map((s) => s.title).join(", ") : "nothing found")));
    }
    case "style": {
      const prefs = d.preferences || [];
      return card(null,
        h("div", { class: "text" }, "Whether the answer follows how you asked to be answered:"),
        prefs.map((c) => h("div", { class: "claim" },
          h("div", { class: "text" }, c.preference),
          h("div", { class: "claim-bar" }, h("span", { class: "val" }, fmt(c.probability)),
            h("span", { class: `chip ${c.followed ? "ok" : "danger"}` }, c.followed ? "followed" : "not followed")))));
    }
    case "screen": {
      if (d.error) return card(null, h("div", { class: "text" }, "Could not read the output first: the hemispheres see it unscreened."), h("div", { class: "sub" }, d.error));
      if (Array.isArray(d.flagged)) return card(null, h("div", { class: "notice danger" }, icon("alert"), h("div", { class: "grow" }, event.summary)));
      return d.flagged
        ? card(d.tool, h("div", { class: "notice danger" }, icon("alert"), h("div", { class: "grow" },
            h("strong", {}, "This output addresses the agent instead of reporting information."),
            h("div", {}, `The left judged it ${fmt(d.probability)} likely to be instructions. The right sees it only fenced off and marked untrusted: it may use the facts, not follow the instructions. The policy gate still applies to anything it does next.`))))
        : card(d.tool, h("div", { class: "text" }, `Information only (p=${fmt(d.probability)} that it addresses the agent).`));
    }
    case "revise":
      return card(null, h("div", { class: "text" }, event.summary.replace(/^revised the answer/, "Revised the answer")),
        event.details.map((line) => h("div", { class: "sub" }, line)));
    case "report": {
      const text = d.report !== undefined ? d.report : event.summary;
      return card(null, h("div", { class: "report" }, text.split(/\n{2,}/).map((p) => h("p", { class: "text" }, p))));
    }
    default:
      return fallback(event, card);
  }
}

function toolCall(option) {
  if (!option.tool) return null;
  return h("span", { class: "tool-call", title: JSON.stringify(option.arguments || {}) },
    h("span", { class: "chip harness" }, `calls ${option.tool}`));
}

function findOption(run, step, id) {
  const propose = run.events.find((e) => e.phase === "propose" && e.step === step);
  const options = propose && propose.data.proposal ? propose.data.proposal.options : [];
  const found = options.find((o) => o.id === id);
  if (found) return found;
  const approve = run.events.find((e) => e.phase === "approve" && e.step === step);
  return approve && approve.data.option && approve.data.option.id === id ? approve.data.option : null;
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

function signOff(run, conversation) {
  const gate = [...run.events].reverse().find((e) => e.phase === "gate" && e.data.decision && e.data.decision.outcome === "flag_human");
  if (!gate) return null;
  const step = gate.step;
  const propose = run.events.find((e) => e.phase === "propose" && e.step === step);
  const evaluation = run.events.find((e) => e.phase === "evaluate" && e.step === step);
  const decision = gate.data.decision;
  const recommended = evaluation && evaluation.data.evaluation ? evaluation.data.evaluation.option_id : null;
  const options = propose && propose.data.proposal ? propose.data.proposal.options : [];
  const note = h("input", { class: "input", type: "text", placeholder: "Add a note for the agent (optional)", "aria-label": "Note" });
  const alone = h("input", { type: "checkbox" });
  const buttons = [];

  const decide = async (body) => {
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await conversation.resume(run, { ...body, note: note.value, alone_next_time: body.approve && alone.checked });
    } catch (error) {
      if (error.status !== 401) toast(error.message, "error");
      buttons.forEach((b) => { b.disabled = false; });
    }
  };
  const button = (label, cls, body) => {
    const b = h("button", { class: `btn small ${cls}`, type: "button", onclick: () => decide(body) }, label);
    buttons.push(b);
    return b;
  };

  return h("div", { class: "signoff" },
    h("div", {}, h("strong", {}, `Step ${step} is waiting for you. `), decision.reasons[decision.reasons.length - 1] || ""),
    options.map((o) => {
      const permit = decision.permits ? decision.permits[o.id] : undefined;
      return h("div", { class: `option ${o.id === recommended ? "chosen" : ""}` },
        h("code", {}, o.id),
        h("span", { class: "desc" }, o.description, toolCall(o),
          o.tool ? h("span", { class: "mono tool-args" }, JSON.stringify(o.arguments || {})) : null,
          o.id === recommended ? h("span", { class: "chip right" }, "the right recommends") : null,
          o.id === decision.left_choice ? h("span", { class: "chip left" }, `the left's pick, p=${fmt(decision.probability)}`) : null,
          permit !== undefined ? h("span", { class: "chip" }, `policy ${fmt(permit)}`) : null),
        button("Approve", "tag", { approve: true, option_id: o.id }));
    }),
    note,
    run.demo ? null : h("label", { class: "check", title: "Each approval is kept as a test case for the left hemisphere: hemisphere-harness eval --signoffs" },
      alone, " What I approve could have been done without asking me"),
    h("div", { class: "row" },
      button("Reject all", "", { approve: false }),
      h("span", { class: "faint" }, "Approving carries out that action and the agent carries on from there. Rejecting sends it back to find another way.")));
}

// ---------------------------------------------------------------------------------------
// The run as a network: layers of neurons from the request to the answer. Options are lit
// by the left's probability, connections are weighted by it, and signals pulse into the
// newest layer while the run is live. Drawn with DOM calls only.

const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined && value !== false) el.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function finalGate(run, step) {
  const gates = run.events.filter((e) => e.phase === "gate" && e.step === step && e.data.decision);
  return gates.length ? gates[gates.length - 1].data.decision : null;
}

function outcomeOf(decision) {
  if (!decision) return { kind: "pending", title: "Being decided" };
  const reason = decision.reasons && decision.reasons.length ? `\n${decision.reasons[decision.reasons.length - 1]}` : "";
  switch (decision.outcome) {
    case "proceed":
      return decision.committed_by === "left"
        ? { kind: "left", title: `The left commits '${decision.option_id}' (p=${fmt(decision.probability)})` }
        : { kind: "right", title: `The right commits '${decision.option_id}'${reason}` };
    case "flag_human":
      return { kind: "human", title: `Waiting for you${reason}` };
    case "revise":
      return { kind: "revise", title: `Nothing done; the right will propose again${reason}` };
    default:
      return { kind: "pending", title: `Back to the right${reason}` };
  }
}

function networkLayers(run) {
  const events = run.events;
  const layers = [{ label: run.resumed_from ? "sign-off" : "request", nodes: [{ kind: "input", title: run.message || run.goal }] }];
  const plan = events.find((e) => e.phase === "plan");
  if (plan) layers.push({ label: "plan", nodes: [{ kind: "right", title: (plan.data.plan && plan.data.plan.situation) || plan.summary }] });
  const human = events.find((e) => e.phase === "approve" || e.phase === "reject");
  if (human) {
    const approved = human.phase === "approve";
    layers.push({ label: "you", nodes: [{ kind: `human ${approved ? "approved" : "rejected"}`, title: approved ? `You approved '${(human.data.option || {}).id}'` : "You rejected the options" }] });
  }

  const steps = [...new Set(events.filter((e) => e.step !== null && e.step !== undefined).map((e) => e.step))];
  for (const step of steps) {
    const propose = events.find((e) => e.phase === "propose" && e.step === step);
    const decide = events.find((e) => e.phase === "decide" && e.step === step);
    const options = (propose && propose.data.proposal && propose.data.proposal.options) || [];
    if (!options.length) continue;
    const probabilities = (decide && decide.data.decision && decide.data.decision.probabilities) || {};
    const decision = finalGate(run, step);
    const settled = decision && decision.outcome !== "escalate_to_right";
    layers.push({
      label: `step ${step}`,
      step,
      nodes: options.map((o) => ({
        kind: "option",
        p: probabilities[o.id],
        chosen: settled && decision.option_id === o.id,
        title: `${o.id}\n${o.description}${probabilities[o.id] !== undefined ? `\nthe left: p=${fmt(probabilities[o.id])}` : ""}`,
        step,
        id: o.id,
      })),
    });
    if (decide || decision) {
      const outcome = outcomeOf(settled ? decision : null);
      layers.push({ label: "", step, outcome: true, nodes: [{ kind: `outcome ${outcome.kind}`, title: outcome.title, step }] });
    }
  }

  const check = events.find((e) => e.phase === "check" && e.data.checks);
  const draft = events.find((e) => e.phase === "draft" && e.data.claims);
  if (check) {
    layers.push({
      label: "check",
      check: true,
      nodes: check.data.checks.map((c) => ({ kind: `claim ${c.verdict}`, p: c.probability, title: `${c.claim.text}\n${c.verdict}, p=${fmt(c.probability)}` })),
    });
  } else if (draft) {
    layers.push({ label: "check", check: true, nodes: draft.data.claims.map((c) => ({ kind: "claim pending", title: c.text })) });
  }
  if (events.some((e) => e.phase === "report")) layers.push({ label: "answer", nodes: [{ kind: "output", title: "The answer" }] });
  return layers;
}

// The weight of a connection: how much signal the source neuron passes on.
function edgeWeight(source, target, layer) {
  if (layer.outcome) return source.p !== undefined ? source.p : 0.3;
  if (target.kind === "output" && source.p !== undefined) return source.p;
  return 0.45;
}

function renderNetwork(run, { onSelect, available = 820 } = {}) {
  const layers = networkLayers(run);
  const PAD_X = 34; // room for the outer layers' labels
  const GAP_X = Math.max(48, Math.min(92, (available - 2 * PAD_X) / Math.max(1, layers.length - 1)));
  const GAP_Y = 34;
  const TOP = 30;
  const tallest = Math.max(...layers.map((l) => l.nodes.length));
  const height = TOP + Math.max(1, tallest) * GAP_Y + 10;
  const width = PAD_X * 2 + (layers.length - 1) * GAP_X;
  const live = run.status === "running";

  layers.forEach((layer, i) => {
    const x = PAD_X + i * GAP_X;
    const n = layer.nodes.length;
    layer.nodes.forEach((node, j) => {
      node.x = x;
      node.y = TOP + (height - TOP - 10) / 2 + (j - (n - 1) / 2) * GAP_Y;
      node.r = node.kind === "option" ? 11 : node.kind.startsWith("claim") ? 8 : 12;
    });
  });

  const edges = svg("g", { class: "edges" });
  for (let i = 1; i < layers.length; i += 1) {
    const layer = layers[i];
    const fresh = live && i === layers.length - 1; // signals travel into the newest layer
    for (const source of layers[i - 1].nodes) {
      for (const target of layer.nodes) {
        const w = Math.max(0, Math.min(1, edgeWeight(source, target, layer)));
        const path = source.chosen && layer.outcome;
        const mid = (source.x + target.x) / 2;
        edges.append(svg("path", {
          d: `M${source.x + source.r} ${source.y} C${mid} ${source.y} ${mid} ${target.y} ${target.x - target.r} ${target.y}`,
          class: `edge${path ? " path" : ""}${fresh ? " fresh" : ""}`,
          "stroke-width": (0.6 + 2.6 * w).toFixed(2),
          "stroke-opacity": (path ? 0.9 : 0.12 + 0.55 * w).toFixed(2),
        }));
      }
    }
  }

  const nodes = svg("g", { class: "nodes" });
  layers.forEach((layer, i) => {
    const last = i === layers.length - 1;
    if (layer.label) {
      nodes.append(svg("text", { x: layer.nodes[0].x, y: 14, class: "layer-label", "text-anchor": "middle" }, layer.label));
    }
    for (const node of layer.nodes) {
      const group = svg("g", {
        class: `neuron ${node.kind}${node.chosen ? " chosen" : ""}${live && last ? " active" : ""}${node.step ? " clickable" : ""}`,
        tabindex: node.step ? 0 : null,
        role: node.step ? "button" : null,
        "aria-label": node.title.split("\n")[0],
      });
      group.append(svg("title", {}, node.title));
      const circle = svg("circle", { cx: node.x, cy: node.y, r: node.r });
      if (node.p !== undefined && node.kind === "option") circle.setAttribute("fill-opacity", (0.12 + 0.88 * node.p).toFixed(2));
      group.append(circle);
      if (node.kind === "option" && node.p !== undefined) {
        group.append(svg("text", { x: node.x, y: node.y + 3.2, "text-anchor": "middle", class: `activation${node.p >= 0.5 ? " on" : ""}` }, Math.round(node.p * 100)));
      }
      if (node.step && onSelect) {
        group.addEventListener("click", () => onSelect(node.step));
        group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(node.step); } });
      }
      nodes.append(group);
    }
  });

  return svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, class: "net", role: "img", "aria-label": "The run drawn as a network, from the request to the answer" }, edges, nodes);
}

// ---------------------------------------------------------------------------------------
// Settings

const SECTIONS = [
  ["right", "Right hemisphere"],
  ["left", "Left hemisphere"],
  ["loop", "Loop and policy"],
  ["tools", "Tools"],
  ["memory", "Memory"],
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
  if (!location.hash.startsWith("#/run/") && !location.hash.startsWith("#/c/")) history.replaceState(null, "", `#/settings/${section}`);
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

  const body = { right: rightSection, left: leftSection, loop: loopSection, tools: toolsSection, memory: memorySection, about: aboutSection }[settings.section]();
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

function secretField(name, label, empty = "Enter your API key") {
  const f = fieldInfo(name);
  const locked = f.source === "environment";
  const removing = settings.draft[name] === null;
  const placeholder = locked ? "Set in your shell" : removing ? "Will be removed when you save" : f.set ? "Saved. Leave blank to keep it." : empty;
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
    const order = ["auto", "ollama", "lmstudio", "llamacpp", "vllm", "anthropic", "openai", "gemini", "nvidia", "xai", "openrouter", "openai-compatible"];
    providers = [...providers].sort((a, b) => (order.indexOf(a.name) + 1 || 99) - (order.indexOf(b.name) + 1 || 99));
    const local = (p) => p.name === "auto" || (p.base_url && p.base_url.startsWith("http://localhost"));
    const groups = [
      ["On your machine", providers.filter(local)],
      ["Hosted", providers.filter((p) => p.base_url && !local(p))],
      ["Other", providers.filter((p) => !p.base_url && !local(p))],
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
    if (p.name === "auto") return "finds the local server that has the model";
    if (!p.base_url) return "any OpenAI-compatible server";
    const host = p.base_url.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    return p.api_key_env ? host : `${host} · no key`;
  }
  return {
    auto: "a local decision model, else Jev, else the mock",
    jev: "hosted · needs JEV_API_KEY",
    ollama: "local decision model · no key",
    systemone: "any /v1/systemone server",
    "llm-experimental": "not calibrated",
    mock: "offline stand-in",
  }[p.name] || p.summary;
}

function modelField(side, provider) {
  const name = side === "right" ? "RIGHT_MODEL" : provider.name === "jev" ? "JEV_MODEL" : "LEFT_MODEL";
  const canList = side === "right" || ["auto", "ollama", "llm-experimental", "systemone"].includes(provider.name);
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
      const notDecision = picker.side === "left" && ["ollama", "auto"].includes(provider.name) && m.decision === false;
      list.append(h("button", {
        class: "picker-item", type: "button", disabled: notDecision,
        title: notDecision ? "Not a decision model. Ollama refuses it at /v1/systemone." : m.id,
        onclick: () => { settings.draft[name] = m.id; settings.picker = null; renderSettings(); },
      }, h("span", {}, m.id),
        m.server ? h("span", { class: "chip" }, providerName(m.server, "right")) : null,
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
  const selectedName = current("RIGHT_PROVIDER") || fieldInfo("RIGHT_PROVIDER").default || "auto";
  const provider = data.right_providers.find((p) => p.name === selectedName || (p.aliases || []).includes(selectedName)) || { name: selectedName, settings: [] };
  const fields = [];
  if (!provider.plugin) {
    fields.push(modelField("right", provider));
    if (provider.api_key_env) fields.push(secretField(provider.api_key_env, `${providerName(provider.name, "right")} API key`));
    if ((provider.settings || []).includes("RIGHT_EFFORT")) fields.push(choiceField("RIGHT_EFFORT", { "": "The model's default" }));
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
            h("div", { class: "form-grid" },
              textField("RIGHT_PRICE_INPUT", { type: "number", placeholder: "unset" }),
              textField("RIGHT_PRICE_OUTPUT", { type: "number", placeholder: "unset" })),
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
    blocks.push(h("p", { class: "muted" }, "Uses the largest decision model Ollama serves on this machine, or the one you name. Without one, Jev if its API key is set, and the mock otherwise."), secretField("JEV_API_KEY", "Jev API key"));
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
      h("div", { class: "card stack" }, boolField("HARNESS_LOCAL_ONLY")),
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
        textField("HARNESS_MAX_STEPS", { type: "number" })),
      answerCheckCard(),
      h("div", { class: "card stack" },
        h("div", { class: "field-label" }, "Tools and searches"),
        boolField("HARNESS_WEB_SEARCH"),
        boolField("HARNESS_FETCH_URL"),
        boolField("HARNESS_SCREEN_TOOL_OUTPUT")),
      notifyCard()));
}

// Browser notifications are this browser's choice, kept in localStorage; the webhook is the server's.
function notifyCard() {
  const supported = "Notification" in window;
  const on = supported && Notification.permission === "granted" && browserNotify();
  const toggle = h("button", {
    class: "btn small", type: "button", disabled: !supported || Notification.permission === "denied",
    onclick: async () => {
      if (on) { setBrowserNotify(false); renderSettings(); return; }
      const answer = await Notification.requestPermission();
      setBrowserNotify(answer === "granted");
      renderSettings();
    },
  }, on ? "Turn off in this browser" : "Notify me in this browser");
  return h("div", { class: "card stack" },
    h("div", { class: "field-label" }, "Notifications"),
    h("div", { class: "row" }, toggle, h("span", { class: "faint" },
      !supported ? "This browser has no notifications." : Notification.permission === "denied" ? "Blocked in this browser's site settings." :
        "When a run waits for your sign-off while this tab is in the background.")),
    secretField("HARNESS_NOTIFY_URL", undefined, "https://hooks.slack.com/services/…"),
    choiceField("HARNESS_NOTIFY_ON", { signoff: "A run waits for my sign-off", all: "Also when a run finishes or fails" }));
}

function browserNotify() {
  try { return localStorage.getItem("hh-notify") === "on"; } catch { return false; }
}

function setBrowserNotify(on) {
  try { localStorage.setItem("hh-notify", on ? "on" : "off"); } catch { /* private mode: nothing to keep */ }
}

function notifyBrowser(run) {
  if (!("Notification" in window) || Notification.permission !== "granted" || !browserNotify() || !document.hidden) return;
  if (run.status !== "needs_human") return;
  const goal = (run.message || run.goal || "").slice(0, 120);
  const n = new Notification("Waiting for your sign-off", { body: goal, tag: run.id });
  n.onclick = () => { window.focus(); location.hash = `#/c/${run.conversation_id}`; n.close(); };
}

const MCP_EXAMPLE = `{
  "mcpServers": {
    "files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/a/folder"]
    },
    "remote": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ..." },
      "approval": "always"
    }
  }
}`;

function toolsSection() {
  const result = h("div", { class: "stack" });
  const check = h("button", { class: "btn", type: "button" }, icon("bolt"), "Check tools");
  check.addEventListener("click", async () => {
    check.disabled = true;
    clear(result).append(h("p", { class: "muted" }, "Connecting to the servers…"));
    try {
      const data = await api("/api/tools");
      clear(result);
      const bySource = new Map();
      for (const tool of data.tools) {
        if (!bySource.has(tool.source)) bySource.set(tool.source, []);
        bySource.get(tool.source).push(tool);
      }
      for (const [source, tools] of bySource) {
        result.append(h("div", { class: "card" },
          h("div", { class: "field-label" }, source, h("span", { class: "faint" }, `${tools.length} tool${tools.length === 1 ? "" : "s"}`)),
          h("div", { class: "tool-list" }, tools.map((t) => h("div", { class: "tool-row" },
            h("code", {}, t.name),
            h("span", { class: `chip ${t.needs_approval ? "warn" : "ok"}` }, t.needs_approval ? "needs your sign-off" : "runs freely"),
            h("div", { class: "faint" }, t.description))))));
      }
      if (!data.tools.length && !Object.keys(data.errors).length && !Object.keys(data.problems || {}).length) {
        result.append(h("p", { class: "muted" },
          data.config ? "The file lists no servers." : "No servers file is set, and both built-ins are turned off in Loop and policy."));
      }
      for (const [server, error] of Object.entries(data.errors)) {
        result.append(h("div", { class: "notice warn" }, icon("alert"), h("div", { class: "grow" }, h("strong", {}, `${server} did not start. `), error)));
      }
      for (const [name, why] of Object.entries(data.problems || {})) {
        result.append(h("div", { class: "notice warn" }, icon("alert"), h("div", { class: "grow" }, h("strong", {}, `${name} is not available. `), why)));
      }
    } catch (error) {
      if (error.status !== 401) clear(result).append(h("div", { class: "notice danger" }, icon("alert"), h("div", { class: "grow" }, error.message)));
    }
    check.disabled = false;
  });
  return h("div", {},
    h("div", { class: "settings-title" }, "Tools"),
    h("p", { class: "settings-sub" }, "Tools become options the right can propose: the built-ins web_search and fetch_url, and anything an MCP server offers. The left still chooses, and a tool that changes anything waits for your sign-off before it runs."),
    h("div", { class: "stack" },
      textField("HARNESS_MCP_CONFIG", { placeholder: "mcp.json" }),
      h("div", { class: "row" }, check, h("span", { class: "faint" }, "Connects to each server, lists its tools, then disconnects.")),
      result,
      h("details", { class: "more" }, h("summary", {}, icon("chevron"), "The file's format"),
        h("div", { class: "stack" },
          h("p", { class: "muted" }, "The same mcpServers format other MCP clients use. Per server, \"approval\" can be \"unless_read_only\" (the default), \"always\" or \"never\", and \"tools\" can list the only tools to offer. Edit the file itself: the page does not change it, because its entries are programs that run on this machine."),
          h("pre", { class: "output" }, MCP_EXAMPLE)))));
}

function choiceField(name, labels = {}) {
  const f = fieldInfo(name);
  const value = current(name) || "";
  const select = h("select", {
    class: "input", id: `f-${name}`, disabled: f.source === "environment",
    onchange: (event) => { settings.draft[name] = event.target.value || null; renderSettings(); },
  }, (f.choices || []).map((choice) => h("option", { value: choice, selected: choice === value }, labels[choice] || choice || "Off")));
  return h("div", { class: "field" },
    h("label", { class: "field-label", for: `f-${name}` }, f.label, sourceBadge(f)),
    select,
    f.help ? h("span", { class: "field-help" }, f.help) : null);
}

function answerCheckCard() {
  const lookup = current("HARNESS_LOOKUP") || "";
  return h("div", { class: "card stack" },
    h("div", { class: "field-label" }, "Answer check"),
    boolField("HARNESS_CHECK_ANSWERS"),
    choiceField("HARNESS_LOOKUP", { "": "Off: the left judges from what it knows", wikipedia: "Wikipedia (no key)", searxng: "SearXNG (your own instance)", brave: "Brave Search (API key)" }),
    lookup === "wikipedia" ? textField("HARNESS_LOOKUP_LANGUAGE", { placeholder: "en" }) : null,
    lookup === "searxng" ? textField("HARNESS_LOOKUP_URL", { placeholder: "http://localhost:8888" }) : null,
    lookup === "brave" ? secretField("BRAVE_API_KEY") : null,
    lookup ? h("div", { class: "notice warn" }, icon("info"), h("div", { class: "grow" },
      "Claims the left is unsure of are sent, as search queries, to this service.")) : null);
}

function memorySection() {
  const list = h("div", { class: "stack" }, h("p", { class: "muted" }, "Loading…"));
  const load = async () => {
    try {
      const data = await api("/api/memory");
      clear(list);
      list.append(h("p", { class: "faint" }, `Kept in ${data.path}`));
      if (!data.notes.length) {
        list.append(h("p", { class: "muted" }, data.enabled ? "Nothing remembered yet." : "Nothing remembered."));
        return;
      }
      for (const note of data.notes) {
        const remove = h("button", { class: "btn small ghost", type: "button" }, icon("trash"), "Forget");
        remove.addEventListener("click", async () => {
          try {
            await api(`/api/memory/${encodeURIComponent(note.id)}`, { method: "DELETE" });
            load();
          } catch (error) {
            if (error.status !== 401) toast(error.message, "error");
          }
        });
        list.append(h("div", { class: "note-row" },
          h("div", { class: "grow" }, h("div", {}, note.text, " ",
            h("span", { class: `chip ${note.kind === "preference" ? "right" : ""}`, title: note.kind === "preference" ? "Always recalled" : "Recalled when a request touches it" }, note.kind || "fact")),
            h("div", { class: "faint" }, relativeTime(note.created), note.source ? ` · from “${note.source.slice(0, 80)}”` : "")),
          remove));
      }
      const all = h("button", { class: "btn small danger", type: "button" }, "Forget everything");
      all.addEventListener("click", async () => {
        if (!confirm("Forget every note?")) return;
        await api("/api/memory", { method: "DELETE" });
        load();
      });
      list.append(h("div", {}, all));
    } catch (error) {
      if (error.status !== 401) clear(list).append(h("div", { class: "notice danger" }, icon("alert"), h("div", { class: "grow" }, error.message)));
    }
  };
  load();
  return h("div", {},
    h("div", { class: "settings-title" }, "Memory"),
    h("p", { class: "settings-sub" }, "With memory on, the right keeps short notes worth remembering, such as your preferences or lasting facts about your work, and the left recalls the ones that bear on each new request. The notes stay on this machine."),
    h("div", { class: "stack" },
      boolField("HARNESS_MEMORY"),
      textField("HARNESS_MEMORY_FILE", { placeholder: "memory.json next to the run history" }),
      h("div", { class: "field-label" }, "What it remembers"),
      list));
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
  const chat = location.hash.match(/^#\/c\/([\w-]+)$/);
  if (chat) {
    showConversation(chat[1]);
    return;
  }
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

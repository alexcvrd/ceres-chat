// ceres-chat v0 page logic. Talks only to this server's /api/*; the server adds the Ceres key.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { lang: "en", exchanges: [], selected: null, busy: false, conversationId: null, profileId: "" };

function newConversationId() {   // the same rule as ceres-client.js: 8-64 of [A-Za-z0-9_-]
  const c = globalThis.crypto;
  return "c-" + (c?.randomUUID ? c.randomUUID().replace(/-/g, "") : String(Date.now()) + Math.random().toString(16).slice(2));
}

function newConversation() {
  state.conversationId = newConversationId();
  state.exchanges = []; state.selected = null;
  const empty = el("div", "empty"); empty.id = "empty";
  const p = el("p", null, "New conversation");
  if (state.profileId) p.append(document.createTextNode(" as "), el("b", null, state.profileId));
  p.append(document.createTextNode(". Follow-ups refer to the earlier turns of this conversation."));
  empty.append(p);
  $("thread").replaceChildren(empty);
  renderEvidence(null);
}

async function loadProfiles() {
  try {
    const body = await (await fetch("/api/profiles")).json();
    for (const p of body.profiles || []) $("persona").add(new Option(`${p.id} (${p.role})`, p.id));
    for (const p of body.profiles || []) [...$("persona").options].find((o) => o.value === p.id).title = p.summary;
  } catch (e) { /* no personas: the picker stays at "no persona" */ }
}

function store(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
function recall(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

// ---------------------------------------------------------------- status
async function refreshStatus() {
  const box = $("status");
  try {
    const s = await (await fetch("/api/status")).json();
    const info = s.info || {};
    const release = info.release_attempt_id || (s.health && s.health.release) || "?";
    const labels = { ready: ["ready", "Ceres ready"], not_ready: ["warn", "Ceres not ready"], down: ["down", "Ceres down"],
                     key_rejected: ["down", "Ceres rejects the key"] };
    const [cls, label] = labels[s.ceres] || ["warn", s.ceres];
    box.className = "status " + cls;
    box.replaceChildren(el("span", "dot"), document.createTextNode(label + (s.ceres === "ready" || s.ceres === "not_ready" ? ` · ${release}` : "")));
    if (s.ceres === "not_ready" && s.health) {
      const parts = Object.entries(s.health).filter(([k, v]) => typeof v === "string" && v !== "ready" && k !== "api").map(([k, v]) => `${k}: ${v}`);
      box.title = parts.join(", ") || "not ready";
    } else {
      box.title = info.route_id ? `route ${info.route_id.slice(0, 12)}…` : s.url || "";
    }
  } catch (e) {
    box.className = "status down"; box.textContent = "chat server unreachable";
  }
}

// ---------------------------------------------------------------- rendering
function decisionPill(ex) {
  if (ex.error) return el("span", "pill error", ex.error.kind.replace("_", " "));
  const c = (ex.ceres.answer || {}).candidate || {};
  const state_ = (ex.ceres.answer || {}).state;
  if (!c.decision) return el("span", "pill error", state_ || "no answer");
  return el("span", "pill " + (c.decision === "ANSWER" ? "answer" : "decline"), c.decision === "ANSWER" ? "ANSWER" : `${c.decision} · ${c.reason_code || ""}`);
}

function citationNumbers(ex) {
  // One number per distinct evidence id, in order of first citation.
  const nums = new Map();
  const c = ((ex.ceres || {}).answer || {}).candidate || {};
  for (const cit of c.citations || []) if (!nums.has(cit.evidence_id)) nums.set(cit.evidence_id, nums.size + 1);
  return nums;
}

function answerText(ex, container) {
  // The answer with a citation mark placed after each cited claim (when the claim occurs verbatim), else listed below.
  const c = ex.ceres.answer.candidate;
  const text = c.user_answer || "";
  const nums = citationNumbers(ex);
  const spots = [];
  for (const cit of c.citations || []) {
    const at = cit.claim ? text.indexOf(cit.claim) : -1;
    if (at >= 0) spots.push({ end: at + cit.claim.length, eid: cit.evidence_id, quote: cit.support_quote });
  }
  spots.sort((a, b) => a.end - b.end);
  let pos = 0;
  const div = el("div", "text");
  for (const s of spots) {
    div.append(document.createTextNode(text.slice(pos, s.end)));
    div.append(citeMark(ex, s.eid, nums.get(s.eid), s.quote));
    pos = s.end;
  }
  div.append(document.createTextNode(text.slice(pos)));
  container.append(div);
  if ((c.citations || []).length) {
    const ul = el("ul", "claims");
    for (const cit of c.citations) {
      const li = el("li");
      li.append(citeMark(ex, cit.evidence_id, nums.get(cit.evidence_id), cit.support_quote), document.createTextNode(` “${cit.support_quote || cit.claim}”`));
      ul.append(li);
    }
    container.append(ul);
  }
  if ((c.missing_information || []).length) {
    container.append(el("div", "muted", "Missing from the library:"));
    const ul = el("ul", "missing");
    for (const m of c.missing_information) ul.append(el("li", null, m));
    container.append(ul);
  }
}

function citeMark(ex, eid, n, quote) {   // `quote`: this citation's own quote, highlighted when the mark is clicked
  const b = el("span", "cite", n ? String(n) : eid);
  b.title = `passage ${eid}`;
  b.addEventListener("click", (e) => { e.stopPropagation(); select(ex, eid, quote); });
  return b;
}

function details(ex) {
  const d = el("div", "details");
  if (ex.ceres) {
    const m = ex.ceres.meta || {};
    const lat = m.latency_ms || {};
    d.append(el("div", null, `latency: ${Object.entries(lat).map(([k, v]) => `${k} ${Math.round(v)} ms`).join(" · ")}`));
    d.append(el("div", null, `release ${m.release_attempt_id} · route ${(m.route_id || "").slice(0, 12)}… · model ${m.model_reported} · mode ${m.serving_mode} · validator ${m.validator_revision}`));
    d.append(el("div", null, `state ${ex.ceres.answer.state} · attempts ${ex.ceres.answer.attempts} · confidence ${(ex.ceres.answer.candidate || {}).confidence}`));
  }
  const mem = ex.ceres && (ex.ceres.meta || {}).memory;
  if (mem) d.append(el("div", null, `memory: conversation ${mem.conversation_id} · turn ${mem.turn} · persona ${mem.profile_id || "none"} · rewrite ${mem.rewrite_status} (${Math.round(mem.rewrite_ms || 0)} ms) · searched: ${mem.retrieval_query}`));
  d.append(el("div", null, `round trip ${Math.round(ex.ms)} ms · exchange ${ex.id || "—"}`));
  const pre = el("pre");
  pre.textContent = JSON.stringify(ex.ceres || ex.error, null, 2);
  d.append(pre);
  return d;
}

function feedback(ex) {
  const f = el("div", "feedback");
  const up = el("button", null, "👍"), down = el("button", null, "👎");
  const note = el("input"); note.placeholder = "comment (optional)"; note.maxLength = 4000;
  const saved = el("span", "saved");
  const send = async (rating) => {
    up.classList.toggle("on", rating === "up"); down.classList.toggle("on", rating === "down");
    try {
      const r = await fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exchange_id: ex.id, rating, comment: note.value.trim() }) });
      saved.textContent = r.ok ? "saved" : "not saved";
    } catch (e) { saved.textContent = "not saved"; }
  };
  for (const [b, r] of [[up, "up"], [down, "down"]]) b.addEventListener("click", (e) => { e.stopPropagation(); send(r); });
  note.addEventListener("click", (e) => e.stopPropagation());
  note.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(up.classList.contains("on") ? "up" : "down"); } });
  f.append(up, down, note, saved);
  return f;
}

function renderAnswer(ex) {
  const a = ex.node;
  a.replaceChildren();
  a.classList.remove("thinking");
  const head = el("div", "head");
  head.append(decisionPill(ex));
  if (ex.ceres) {
    const lat = ((ex.ceres.meta || {}).latency_ms || {}).total;
    if (lat) head.append(el("span", null, `${(lat / 1000).toFixed(1)} s`));
    head.append(el("span", null, `${(ex.ceres.packet.contexts || []).length} passages`));
  }
  a.append(head);
  const mem = ex.ceres && (ex.ceres.meta || {}).memory;
  if (mem) {
    if (mem.turn) head.append(el("span", null, `turn ${mem.turn}`));
    if (mem.standalone_question && mem.standalone_question.trim() !== ex.question.trim())
      a.append(el("p", "understood", `Understood as: ${mem.standalone_question}`));
    if (mem.rewrite_status === "fallback") a.append(el("p", "understood", "Ceres could not link this to the earlier turns."));
  }
  if (ex.error) {
    a.append(el("div", "text", ex.error.detail));
  } else if ((ex.ceres.answer || {}).candidate) {
    answerText(ex, a);
  } else {
    a.append(el("div", "text", `Ceres returned no answer (state ${(ex.ceres.answer || {}).state}).`));
  }
  a.append(details(ex));
  if (ex.id) a.append(feedback(ex));
  a.addEventListener("click", () => select(ex));
}

function highlight(body, text, quote) {
  body.textContent = "";
  if (!quote) { body.textContent = text; return false; }
  let at = text.indexOf(quote), len = quote.length;
  if (at < 0) {   // tolerate whitespace differences
    const esc = quote.trim().split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+");
    const m = esc && new RegExp(esc).exec(text);
    if (m) { at = m.index; len = m[0].length; }
  }
  if (at < 0) { body.textContent = text; return false; }
  const mk = el("mark", null, text.slice(at, at + len));
  body.append(document.createTextNode(text.slice(0, at)), mk, document.createTextNode(text.slice(at + len)));
  return mk;
}

function renderEvidence(ex, focus, focusQuote) {
  const list = $("evidence-list");
  list.replaceChildren();
  $("evidence-empty").hidden = !!(ex && ex.ceres);
  if (!ex || !ex.ceres) return;
  const nums = citationNumbers(ex);
  const quotes = {};
  for (const cit of (ex.ceres.answer.candidate || {}).citations || []) (quotes[cit.evidence_id] ||= []).push(cit.support_quote);
  let focusNode = null, focusMark = null;
  for (const c of ex.ceres.packet.contexts || []) {
    const box = el("div", "ctx" + (nums.has(c.evidence_id) ? " cited" : ""));
    const meta = el("div", "meta");
    meta.append(el("span", "eid", c.evidence_id));
    if (nums.has(c.evidence_id)) meta.append(el("span", "cite", String(nums.get(c.evidence_id))));
    const src = ((ex.ceres.meta || {}).sources || []).find((x) => x.evidence_id === c.evidence_id);
    if (src && src.title) {
      const who = (src.authors || []).length > 2 ? `${src.authors[0]} et al.` : (src.authors || []).join(", ");
      const t = el("span", "srctitle", [src.title, who, src.page_label].filter(Boolean).join(" · "));
      t.title = c.parent_id || c.chunk_id;            // the raw id stays one hover away
      meta.append(t);
    } else {
      meta.append(el("span", "src", c.parent_id || c.chunk_id));
    }
    for (const w of c.warnings || []) meta.append(el("span", "warn", w.replace(/_/g, " ")));
    const body = el("div", "body");
    const mk = highlight(body, c.evidence_text || "", (c.evidence_id === focus && focusQuote) || (quotes[c.evidence_id] || [])[0]);
    box.append(meta, body);
    list.append(box);
    if (c.evidence_id === focus) { focusNode = box; focusMark = mk; }
  }
  if (focusNode) {
    focusNode.scrollIntoView({ block: "nearest", behavior: "smooth" });
    focusNode.classList.add("flash");
    if (focusMark) focusMark.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

function select(ex, focus, focusQuote) {
  for (const e of state.exchanges) e.node.classList.toggle("selected", e === ex);
  state.selected = ex;
  renderEvidence(ex, focus, focusQuote);
}

// ---------------------------------------------------------------- asking
async function ask(question) {
  $("empty")?.remove();
  const thread = $("thread");
  const q = el("div", "q", question);
  q.append(el("span", "lang-tag", state.lang));
  const a = el("div", "a thinking", "Ceres is reading the library");
  thread.append(q, a);
  a.scrollIntoView({ block: "end", behavior: "smooth" });
  const ex = { question, lang: state.lang, node: a };
  const t0 = performance.now();
  const conversationId = state.conversationId;
  state.busy = true; $("send").disabled = true;
  try {
    try {
      const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, lang: state.lang, conversation_id: conversationId,
                               ...(state.profileId ? { profile_id: state.profileId } : {}) }) });
      const body = await r.json();
      if (r.ok) { ex.id = body.exchange_id; ex.ceres = body.ceres; }
      else if (body.error) ex.error = body.error;
      else ex.error = { kind: "invalid_request", status: r.status, detail: JSON.stringify(body.detail || body) };
    } catch (e) {
      ex.error = { kind: "chat_server_down", status: 0, detail: "The chat server did not respond." };
    }
    ex.ms = performance.now() - t0;
    if (conversationId !== state.conversationId) return;   // the user left this conversation: drop the stale answer
    state.exchanges.push(ex);
    try {
      renderAnswer(ex);
      select(ex);
    } catch (e) {
      a.classList.remove("thinking");
      a.textContent = "This answer could not be displayed.";
    }
    if (ex.error) refreshStatus();
  } finally {
    state.busy = false; $("send").disabled = false;
  }
}

function init() {
  const saved = recall("ceres-chat-lang");
  if (saved === "en" || saved === "ro") state.lang = saved;
  for (const b of document.querySelectorAll(".lang button")) {
    b.classList.toggle("on", b.dataset.lang === state.lang);
    b.addEventListener("click", () => {
      state.lang = b.dataset.lang; store("ceres-chat-lang", state.lang);
      for (const o of document.querySelectorAll(".lang button")) o.classList.toggle("on", o === b);
    });
  }
  const details = recall("ceres-chat-details") === "1";
  $("details-toggle").checked = details; document.body.classList.toggle("show-details", details);
  $("details-toggle").addEventListener("change", (e) => {
    document.body.classList.toggle("show-details", e.target.checked); store("ceres-chat-details", e.target.checked ? "1" : "0");
  });
  $("ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("question").value.trim();
    if (!q || state.busy) return;
    $("question").value = "";
    ask(q);
  });
  $("question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ask").requestSubmit(); }
  });
  $("persona").addEventListener("change", (e) => { state.profileId = e.target.value; newConversation(); });   // one persona per conversation
  $("new-conv").addEventListener("click", newConversation);
  state.conversationId = newConversationId();
  loadProfiles();
  refreshStatus();
  setInterval(refreshStatus, 30000);
}

init();

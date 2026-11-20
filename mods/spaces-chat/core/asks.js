// What Claude asks the person through the PermissionRequest hook (ask-hook.sh), in words for
// the chat's card, and the hook's answers. Nothing is allowed unread, and nothing is too long
// to answer in the chat: the card shows the whole request, scrolling inside itself.
// Shared core: the source is core/asks.js (the window's mods, the service and the web app all use it);
// bin/sync-shared copies it into each mod in lib/COPIES. Edit core/asks.js, never a copy, then run bin/sync-shared.

// A card shows up to this many lines (the whole of any real request), each up to LINE characters.
const SHOWN = 2000, LINE = 2000;
// An option's preview, at most this many characters.
const PREVIEW = 8000;
const clip = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
const base = (p) => String(p ?? "").split("/").filter(Boolean).pop() ?? "";
const firstLine = (s) => String(s ?? "").split("\n")[0];
// A file as the person knows it: from the agent's folder, else from home, else whole; just its
// name when that would be long (a reason is at most 40 characters).
function rel(p, cwd) {
  const f = String(p ?? "");
  const home = (/^(\/home\/[^/]+|\/Users\/[^/]+|\/root)(?=\/)/.exec(f) ?? [])[0];
  const r = cwd && f.startsWith(`${cwd}/`) ? f.slice(cwd.length + 1) : home ? `~${f.slice(home.length)}` : f;
  return r.length > 32 ? base(f) : r;
}
const host = (u) => { try { return new URL(String(u)).host || String(u); } catch { return String(u ?? ""); } };

export const loginId = (id) => /^t[1-9][0-9bcdfghjkmnpqrstvwxyz]*\.login\.[0-9]{10}\.[1-9][0-9]*$/.test(id);
export const liveLoginId = (id, now = Date.now()) => loginId(id) &&
  now >= Number(id.split(".")[2]) * 1000 && now - Number(id.split(".")[2]) * 1000 < 1_800_000;

export const loginAnswer = (confirmed) => JSON.stringify({ login: confirmed ? "confirmed" : "canceled" });
export const LOGIN_CHOICES = ["I signed in", "Cancel"];

export const LOGIN_WAITING_SH = `login_waiting() {
  case "$1" in *.login.*.*) ;; *) return 0 ;; esac
  p=\${1##*.}; stamp=\${1%.*}; stamp=\${stamp##*.}
  case "$p:$stamp" in *[!0-9:]*|:*|*:) return 1 ;; esac
  [ "\${#stamp}" -eq 10 ] && [ "$p" -gt 0 ] || return 1
  age=$(( $(date +%s) - stamp ))
  [ "$age" -ge 0 ] && [ "$age" -lt 1800 ] && kill -0 "$p" 2>/dev/null
}`;

// $1 the folder, $2 the id, $3 how it went ("" for none), $4 the answer ("" is no decision: the terminal asks).
// Exit 0 sent, 3 no longer waiting, 4 answered already, 5 couldn't write.
export const ANSWER_ONCE = `${LOGIN_WAITING_SH}
d=$1; id=$2; req="$d/req/$id.json"; ans="$d/ans/$id.json"
[ -f "$req" ] && login_waiting "$id" || exit 3
kill -0 "\${id##*.}" 2>/dev/null || exit 3
[ -e "$ans" ] && exit 4
mkdir -p "$d/ans" "$d/end" || exit 5
tmp="$d/ans/.$id.$$.tmp"
printf '%s' "$4" > "$tmp" || { rm -f "$tmp"; exit 5; }
if ln "$tmp" "$ans" 2>/dev/null; then rm -f "$tmp"; else rm -f "$tmp"; exit 4; fi
[ -z "$3" ] || { printf '%s\\n' "$3" > "$d/end/$id.tmp" && mv "$d/end/$id.tmp" "$d/end/$id"; }
exit 0`;

export function loginTarget(v) {
  if (v?.kind === "browser" || v?.kind === "terminal") return { kind: v.kind };
  if (v?.kind !== "site" || typeof v.origin !== "string" || v.origin.length > 253 ||
      !/^https?:\/\/(?:[a-z0-9.-]+|\[[0-9a-f:]+\])(?::[0-9]{1,5})?\/?$/i.test(v.origin)) return null;
  try {
    const url = new URL(v.origin);
    return url.port === "0" || !url.hostname.replace(/\./g, "") ? null : { kind: "site", origin: url.origin };
  } catch { return null; }
}

// Herdr terminal addresses are stable within a core. The start time also rejects a replaced
// session at that address. The task key comes from the terminal record, never from a card action.
export function ownsLogin(req, terminal, id) {
  return Boolean(liveLoginId(id) && req?.tool_name === "HerdrLogin" && loginTarget(req.tool_input) &&
    terminal?.state === "live" && terminal.address?.split("/").at(-1) === id.split(".")[0] &&
    terminal.created_ms > 0 && terminal.created_ms === req.owner?.created &&
    (terminal.labels?.["herdr.task"] || null) === req.owner?.task &&
    (req.tool_input.kind !== "browser" || Boolean(req.owner.task)) &&
    !["reviewer", "helper"].includes(terminal.labels?.["herdr.role"]));
}

// Lines of detail, and whether some had to be left out (past SHOWN lines or LINE characters:
// the card says so, the terminal has the rest).
function lines(...parts) {
  const all = parts.filter((p) => p !== undefined && p !== null && p !== "").flatMap((p) => String(p).split("\n"));
  const long = all.some((l) => l.length > LINE);
  return { detail: all.slice(0, SHOWN).map((l) => clip(l, LINE)).join("\n"), cut: all.length > SHOWN || long };
}
// An MCP server as people know it: "executor-me" is Executor, "claude_ai_Gmail" is Gmail.
function app(server) {
  const w = String(server).replace(/^(claude_ai|plugin)_/i, "").replace(/[-_](me|mcp|server)$/i, "").replace(/[-_]+/g, " ").trim();
  return w ? w[0].toUpperCase() + w.slice(1) : String(server);
}

// Cursor's own Allow always for a permission its bridge asks (mods/tasks/bin/herdr_task/cursor_chat.py, SHEP-18): the
// request's cursor_always, { option (Cursor's exact optionId), name ("Allow always"), scope (what it adds to Cursor's
// allowlist, in words) }, only when Cursor offers it and what it adds can be said. -> { option, label, scope } or null.
// Claude Code's and Codex's requests never carry it.
function cursorAlways(req) {
  const a = req?.herdr_agent === "cursor" ? req?.cursor_always : null;
  if (!a || typeof a !== "object") return null;
  const ok = (v, n) => typeof v === "string" && v.trim() && v.length <= n;
  if (!ok(a.option, 200) || !ok(a.scope, 400)) return null;
  return { option: a.option, label: ok(a.name, 80) ? a.name.trim() : "Allow always", scope: a.scope.trim() };
}

// { title, detail, cut, kind: "permission" | "question" | "plan", options?, question?, always? (Cursor's Allow always:
// cursorAlways) }
export function words(req) {
  const w = wordsOf(req);
  const always = w.kind === "permission" ? cursorAlways(req) : null;
  return always ? { ...w, always } : w;
}

function wordsOf(req) {
  const tool = String(req?.tool_name ?? "");
  const v = req?.tool_input && typeof req.tool_input === "object" ? req.tool_input : {};
  const cwd = String(req?.cwd ?? "");
  switch (tool) {
    case "HerdrLogin": {
      const login = loginTarget(v);
      const detail = login?.kind === "site" ? login.origin : login?.kind === "browser" ? "Sign in in this task’s Browser." :
        login?.kind === "terminal" ? "Sign in in the requesting terminal." : "The destination is unavailable.";
      return { kind: "login", title: "Sign-in needed", detail, login, cut: false };
    }
    case "AskUserQuestion": {
      // Each question with its options: the chat's question card answers them all at once.
      const questions = (Array.isArray(v.questions) ? v.questions : []).map((q) => ({
        text: String(q?.question ?? ""), header: String(q?.header ?? ""), multi: q?.multiSelect === true,
        options: (Array.isArray(q?.options) ? q.options : []).map((o) => {
          const label = typeof o === "string" ? o : String(o?.label ?? "");
          // its preview (SHEP-139): a mockup, code or a diagram the agent drew to compare the options, as its prompt shows it
          const preview = typeof o === "object" && typeof o?.preview === "string" ? clip(o.preview, PREVIEW) : "";
          return { label, name: label.replace(/\s*\(recommended\)\s*$/i, ""), recommended: /\(recommended\)\s*$/i.test(label), description: typeof o === "object" ? o?.description ?? "" : "", ...(preview ? { preview } : {}) };
        }).filter((o) => o.label),
      })).filter((q) => q.text);
      return { kind: "questions", title: questions[0]?.text ?? "A question", detail: "", questions, cut: false };
    }
    case "ExitPlanMode": {
      const l = lines(v.plan);
      return { kind: "plan", title: "Plan to approve", ...l };
    }
    case "Bash": return { kind: "permission", title: `Run: ${clip(firstLine(v.command), 120)}`, ...lines(v.description, v.command) };
    case "Write": return { kind: "permission", title: `Write ${rel(v.file_path, cwd)}`, ...lines(v.file_path, v.content) };
    case "Edit": return { kind: "permission", title: `Edit ${rel(v.file_path, cwd)}`, ...lines(v.file_path,
      ...String(v.old_string ?? "").split("\n").map((l) => `- ${l}`), ...String(v.new_string ?? "").split("\n").map((l) => `+ ${l}`)) };
    case "MultiEdit": return { kind: "permission", title: `Edit ${rel(v.file_path, cwd)}`, ...lines(v.file_path,
      ...(Array.isArray(v.edits) ? v.edits : []).flatMap((e) => [...String(e?.old_string ?? "").split("\n").map((l) => `- ${l}`), ...String(e?.new_string ?? "").split("\n").map((l) => `+ ${l}`)])) };
    case "NotebookEdit": return { kind: "permission", title: `Edit ${rel(v.notebook_path, cwd)}`, ...lines(v.notebook_path, v.new_source) };
    case "Read": return { kind: "permission", title: `Read ${rel(v.file_path, cwd)}`, ...lines(v.file_path) };
    case "Glob": case "Grep": return { kind: "permission", title: `Search for ${clip(v.pattern, 80)}`, ...lines(v.path) };
    case "WebFetch": return { kind: "permission", title: `Fetch ${clip(host(v.url), 100)}`, ...lines(v.url, v.prompt) };
    case "WebSearch": return { kind: "permission", title: `Search the web: ${clip(v.query, 100)}`, detail: "", cut: false };
  }
  const mcp = /^mcp__(.+?)__(.+)$/.exec(tool);
  const json = Object.keys(v).length ? JSON.stringify(v, null, 1) : "";
  // A tool of an app (MCP): the app's name up top, the tool and its input below.
  return { kind: "permission", title: mcp ? `Use ${app(mcp[1])}` : `Use ${tool || "a tool"}`, ...(mcp ? lines(mcp[2], json) : lines(json)) };
}

const out = (decision) => JSON.stringify({ hookSpecificOutput: { hookEventName: "PermissionRequest", decision } });
export const allow = () => out({ behavior: "allow" });
// Cursor's Allow always (a card's, only where words(req).always offers it): the decision names Cursor's exact option,
// which its bridge picks only for the request that offered it. An older bridge reads it as Allow (once), never more.
// A permission card's choices on the web (service/compat.ts question()): 0 Allow (once), 1 Deny, and this one, Cursor's
// Allow always, where words(req).always offers it. Never renumbered: a page drawn before an update means the same.
export const ALWAYS_CHOICE = 2;
export const allowAlways = (req) => { const a = cursorAlways(req); return a ? out({ behavior: "allow", cursorOption: a.option }) : null; };
export const deny = (message = "The person declined this in the chat.") => out({ behavior: "deny", message });
// A question's answer goes back as the tool's input with `answers`; Claude then goes on.
export const answer = (req, label) => out({ behavior: "allow", updatedInput: { ...(req.tool_input ?? {}), answers: { [String(req.tool_input?.questions?.[0]?.question ?? "")]: label } } });
// All of a question card's answers: { [question text]: answer } (several picks joined with ", ",
// "Other" as written), as Claude's own prompt would give them.
// annotations (SHEP-139): { [question text]: { preview?, notes? } }, as Claude's prompt adds them: the preview of the
// option picked, and the person's note.
export const answers = (req, map, annotations) => out({ behavior: "allow", updatedInput: { ...(req.tool_input ?? {}), answers: map,
  ...(annotations && Object.keys(annotations).length ? { annotations } : {}) } });
// The annotations of a question card's answers: each question's picked option's preview (one pick), and a note for the
// first question when the person wrote one. w: words(req); map: as answers() takes it.
export function annotationsOf(w, map, note) {
  const out = {};
  (w?.questions ?? []).forEach((q, i) => {
    const a = {};
    const o = q.multi ? null : q.options.find((x) => x.name === map?.[q.text]);
    if (o?.preview) a.preview = o.preview;
    if (i === 0 && note && String(note).trim()) a.notes = String(note).trim();
    if (Object.keys(a).length) out[q.text] = a;
  });
  return out;
}
// "Chat about this": no answers; Claude hears the person wants to talk it over first.
export const chatAbout = () => deny("The person wants to talk about these questions before answering. Don't ask them again now: wait for their message in the chat and answer it.");
// "Dismiss" (or Esc on the card): closed without an answer, as Esc in Claude's own prompt, except Claude goes on
// rather than stopping: it hears the person declined and carries on without it. kind: the card's (words().kind).
export const DISMISSED = "The person dismissed this without answering";
export const dismiss = (kind) => deny(kind === "questions"
  ? `${DISMISSED}. Don't ask these questions again now: carry on with your best judgment, and say what you assumed.`
  : kind === "plan"
    ? `${DISMISSED}: the plan isn't approved. Stay in plan mode and wait for the person's message.`
    : `${DISMISSED}: don't do it. Carry on without it, or another way.`);
// No decision: Claude shows its own prompt in the terminal.
export const release = () => "";

// How a card's answer went, for its quiet line ("Allowed in the chat"); null for no decision (the terminal asks
// instead). ask: { words }, text: the hook's answer, where: "in the chat", "in the task's chat"...
export function howAnswered(ask, text, where) {
  if (ask.words.kind === "login") {
    try {
      const login = JSON.parse(text).login;
      if (login === "confirmed") return "Sign-in confirmed " + where;
      if (login === "canceled") return "Sign-in canceled " + where;
    } catch {}
    return null;
  }
  let d = null;
  try { d = JSON.parse(text).hookSpecificOutput?.decision; } catch {}
  if (!d) return null;
  const plan = ask.words.kind === "plan";
  if (d.behavior === "deny" && String(d.message ?? "").startsWith(DISMISSED)) return `Dismissed ${where}`;
  if (d.behavior === "allow") return `${plan ? "Approved" : d.updatedInput ? "Answered" : d.cursorOption ? "Allowed always" : "Allowed"} ${where}`;
  if (ask.words.kind === "questions") return `Left to talk over ${where}`;
  return plan ? `Sent back to planning ${where}` : `Denied ${where}`;
}

// ---- Codex's own questions (request_user_input) ----------------------------------------------
// Codex gives no hook for its question tool (its PermissionRequest hook is for approvals only), so its card answers on
// its screen, as a person would (SHEP-91): each question in turn, a number key picks a choice (and goes on to the next;
// the last one sends them all), and an answer of one's own goes in the notes of "None of the above" (down to it, tab,
// the words pasted and Enter). Only while the screen shows the questions the card was drawn from.

/** request_user_input's arguments as the card's questions: [{ text, header, choices: [{ label, description? }] }]. */
export function codexQuestions(args) {
  let a = args;
  try { a = typeof a === "string" ? JSON.parse(a) : a; } catch { return []; }
  return (Array.isArray(a?.questions) ? a.questions : []).map((q) => ({
    text: String(q?.question ?? ""), header: String(q?.header ?? ""),
    choices: (Array.isArray(q?.options) ? q.options : []).map((o) => (typeof o === "string" ? { label: o } : { label: String(o?.label ?? ""), ...(o?.description ? { description: String(o.description) } : {}) })).filter((o) => o.label),
  })).filter((q) => q.text);
}

/** A choice as the card shows it: "(Recommended)" as its tag, as Claude's. */
export const codexChoice = (c) => {
  const rec = /\s*\(recommended\)\s*$/i.test(c.label);
  return { label: c.label.replace(/\s*\(recommended\)\s*$/i, ""), ...(c.description ? { description: c.description } : {}), ...(rec ? { recommended: true } : {}) };
};

/** Whether Codex's screen shows these questions' picker at its first question, none answered yet. */
export function codexOnScreen(screen, questions) {
  const s = String(screen ?? "").replace(/\s+/g, " ");
  const q = questions?.[0];
  if (!q) return false;
  const n = questions.length;
  const one = (w) => s.includes(String(w).replace(/\s+/g, " ").trim());
  return new RegExp(`Question 1/${n} \\(${n} unanswered\\)`).test(s) && one(q.text) && q.choices.every((c) => one(c.label));
}

/** The steps that answer them: [{ keys: [...] } | { paste: words }], one answer per question, each its choice's label
 *  or the person's own words. null when an answer is missing. */
export function codexSteps(questions, answers) {
  if (!Array.isArray(answers) || answers.length !== questions.length) return null;
  const steps = [];
  for (const [i, q] of questions.entries()) {
    const said = String(answers[i] ?? "").trim();
    if (!said) return null;
    const k = q.choices.findIndex((c) => c.label === said || codexChoice(c).label === said);
    if (k >= 0 && k < 9) steps.push({ keys: [String(k + 1)] });
    else if (k >= 0) steps.push({ keys: [...Array(k).fill("down"), "enter"] });
    // "None of the above", its notes: the words pasted, then Enter (Codex hears "None of the above" and the note)
    else steps.push({ keys: [...Array(q.choices.length).fill("down"), "tab"] }, { paste: said });
  }
  return steps;
}

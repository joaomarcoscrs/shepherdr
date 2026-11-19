// Toasts, as the window's (mods/toasts): the last three, each a dot in its level's color, its title on one line, its
// body wrapped to at most 3 lines, an X on hover; a click runs its action and closes it; each goes after its level's
// time, never while the pointer is on them. mods/toasts draws them in the window, web/plugins/alerts in the web app;
// both read these rules and words from here.
// Shared core: the source is core/toasts.js; bin/sync-shared copies it into the mods in lib/COPIES. Edit it here, never
// a copy.

import { REASONS } from "./needs.js";
import { words, allow, allowAlways, deny } from "./asks.js";

// How many show at once, how wide they are, and how many lines a body gets.
export const SHOWN = 3;
export const WIDTH = 360;
export const LINES = 3;
// A body is cut to this many characters (3 lines at the toast's width).
export const BODY_MAX = 140;
// How long each level stays (ms); the rest stay 6 s.
export const TIMEOUT = { alert: 20000, error: 12000, warning: 10000 };
export const TIMEOUT_DEFAULT = 6000;
export const timeoutOf = (n) => n.timeout ?? TIMEOUT[n.level] ?? TIMEOUT_DEFAULT;
// Kept at most this long, and at most this many besides a new one.
export const KEEP_MS = 120000;
export const KEEP = 4;

// A toast's dot: red for an alert or an error, amber a warning, green done, the accent otherwise.
export const tone = (level) => (level === "alert" || level === "error" ? "red" : level === "warning" ? "amber" : level === "success" ? "green" : "accent");

export const clip = (s, n) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s);

// One title format for what an agent says: "HERD-4 · Needs you", "~ Assistant · Finished".
export const NEEDS_YOU = "Needs you";
export const FINISHED = "Finished";
export const toastTitle = (who, what) => `${who} · ${what}`;
// Who an Assistant is, by its space's name ("tt-repo Assistant"); a task's agent is its task's ref ("HERD-4"), on
// another machine with that machine's name, as the chat names an agent there (core/chat-view.js): "HERD-4 on vm".
export const assistantWho = (spaceName) => (spaceName ? `${spaceName} Assistant` : "Assistant");
export const taskWho = (ref, machine) => (machine ? `${ref} on ${machine}` : ref);

// Several toasts at once, in one line (a phone shows one toast): "2 tasks need you" when each is a task needing
// you, "2 need you" when an Assistant is among them, else "3 updates".
export function together(list) {
  const n = list.length;
  if (list.every((x) => x.need)) return `${n} ${list.every((x) => x.task) ? "tasks " : ""}need you`;
  return `${n} updates`;
}

// Why an agent needs the person, as a Needs you toast's body says it (the web app's and the service's need: { question?:
// { kind, title }, secret?: { name }, text? }): its question, a plan to approve, the secret it asks for, or what it
// last said; else the standard reason (core/needs.js).
export function needReason(n) {
  if (n?.question) return n.question.kind === "Plan" ? REASONS.plan : n.question.title || REASONS.question;
  if (n?.secret) return `Asks for ${n.secret.name}`;
  return n?.text || REASONS.question;
}

// The list with a new toast added (newest last): older than KEEP_MS go, and only the last KEEP stay beside it. One with
// the same key (one event: an agent needing you, told twice as it learns more) takes the old one's place and id. One
// that says more about the same thing (n.covers: keys, e.g. a permission's toast covering its agent's "Needs you") takes
// those toasts' place: one toast per thing, whichever was told first (lib/ui.js toast() does the same in the window).
export function withToast(list, n, at = Date.now(), id = `${at.toString(36)}${Math.random().toString(36).slice(2, 6)}`) {
  const same = n.key ? (list ?? []).find((x) => x.key === n.key) : null;
  const covered = (x) => Boolean(x.key) && Array.isArray(n.covers) && n.covers.includes(x.key);
  const kept = (list ?? []).filter((x) => x !== same && !covered(x) && at - x.at < KEEP_MS).slice(-KEEP);
  return [...kept, { ...n, level: n.level ?? "info", id: same?.id ?? id, at }];
}

// The toasts left at `now`: each goes after its time.
export const unexpired = (list, now = Date.now()) => (list ?? []).filter((n) => now - n.at < timeoutOf(n));

// ---- a permission's choices on its toast (SHEP-19) -----------------------------------------------------------------
// A toast about one waiting request (an agent's PermissionRequest hook, or Cursor's bridge: core/asks.js) carries only
// the request's id; the toast reads the request itself and offers the choices its chat card offers (Allow or Allow once,
// Deny, and Cursor's own Allow always exactly where the card offers it), only when what is allowed fits the toast whole:
// the exact command, path, address or input in the terminal's font, every character shown, each line at most ASK_COLS
// characters wide (drawn as is, never wrapped or clipped by the toast). Anything else (a plan, questions, an edit or a
// write, a request too long, cut, unreadable, or with characters a toast can't show plainly) gets a Review that opens
// its chat card instead. Nothing here decides: each choice is the person's click, and askAnswer() is the card's own answer.


// A line of the exact text: at most this many characters (the toast's text column in a 12px terminal font, with room
// to spare), and at most this many lines for what is allowed, and for what Allow always adds.
export const ASK_COLS = 36;
export const ASK_LINES = 5;
export const ASK_SCOPE_LINES = 4;
// Choices take no click until they have shown this long: a toast that appears under a moving pointer can't be answered
// by a click meant for something else.
export const ASK_ARM_MS = 800;
// What a Deny from a toast tells the agent.
export const ASK_DENIED = "The person declined this from a notification.";

// Printable ASCII and line breaks only: no tabs, control or invisible characters, no right-to-left marks or look-alike
// letters, so what shows is what is allowed.
const plain = (s) => /^[\x20-\x7e\n]*$/.test(s);

// Text as the toast's lines, each at most `cols` characters, broken after a space where it can and anywhere where it
// can't; every character kept, in order (joined back, the lines are the text).
export function askLines(text, cols = ASK_COLS) {
  const out = [];
  for (const line of String(text).split("\n")) {
    let rest = line;
    if (!rest) { out.push(""); continue; }
    while (rest.length > cols) {
      const sp = rest.lastIndexOf(" ", cols - 1);
      const cut = sp > 0 ? sp + 1 : cols;
      out.push(rest.slice(0, cut));
      rest = rest.slice(cut);
    }
    if (rest) out.push(rest);
  }
  return out;
}

const app = (server) => { const w = String(server).replace(/^(claude_ai|plugin)_/i, "").replace(/[-_](me|mcp|server)$/i, "").replace(/[-_]+/g, " ").trim(); return w ? w[0].toUpperCase() + w.slice(1) : String(server); };
const str = (v) => (typeof v === "string" ? v : "");

// What is allowed, whole: { lead (what kind, e.g. "Run"), text (the exact command, path, ...) }, or null when the toast
// can't say it whole.
function subject(req) {
  const tool = String(req?.tool_name ?? "");
  const v = req?.tool_input && typeof req.tool_input === "object" ? req.tool_input : {};
  if (tool === "Bash") return str(v.command).trim() ? { lead: "Run", text: str(v.command) } : null;
  if (tool === "Read") return str(v.file_path) ? { lead: "Read", text: str(v.file_path) } : null;
  if (tool === "WebFetch") return str(v.url) ? { lead: "Fetch", text: [str(v.url), str(v.prompt)].filter(Boolean).join("\n") } : null;
  if (tool === "WebSearch") return str(v.query) ? { lead: "Search the web", text: str(v.query) } : null;
  if (tool === "Grep" || tool === "Glob") return str(v.pattern) ? { lead: "Search", text: [str(v.pattern), str(v.path) && `in ${str(v.path)}`].filter(Boolean).join("\n") } : null;
  const mcp = /^mcp__(.+?)__(.+)$/.exec(tool);
  if (mcp) return { lead: `Use ${app(mcp[1])}`, text: [mcp[2], Object.keys(v).length ? JSON.stringify(v) : ""].filter(Boolean).join("\n") };
  return null;
}

// A request as its toast shows it: { kind: "choices", lead, lines, choices: [{ id, label, look }], always?: { label,
// lines } } when it can be answered there, else { kind: "review", reason } (its chat card answers it). req: the
// request as read (null: unreadable).
export function askToast(req) {
  if (!req || typeof req !== "object") return { kind: "review", reason: "Claude asks for something the chat can't read" };
  const w = words(req);
  const reason = w.kind === "plan" ? "Plan to approve" : String(w.title ?? "");
  if (w.kind !== "permission" || w.cut) return { kind: "review", reason };
  const s = subject(req);
  if (!s || !s.text.trim() || !plain(s.text)) return { kind: "review", reason };
  const lines = askLines(s.text);
  if (lines.length > ASK_LINES) return { kind: "review", reason };
  // Cursor's Allow always, only with what it adds shown whole too (else the toast has no Allow always: the card has it)
  let always = null;
  if (w.always && allowAlways(req)) {
    const scope = `${w.always.label}: ${w.always.scope}.`;
    const sl = plain(scope) ? askLines(scope) : [];
    if (!sl.length || sl.length > ASK_SCOPE_LINES) return { kind: "review", reason };
    always = { label: w.always.label, lines: sl };
  }
  const choices = [{ id: "allow", label: always ? "Allow once" : "Allow", look: "primary" }, { id: "deny", label: "Deny", look: "secondary" }];
  if (always) choices.push({ id: "always", label: always.label, look: "secondary" });
  return { kind: "choices", lead: s.lead, lines, choices, ...(always ? { always } : {}) };
}

// The hook's answer for a toast's choice, made from the request as read again just before it goes: exactly the chat
// card's (core/asks.js), or null when that request no longer offers this choice on a toast.
export function askAnswer(req, choice) {
  const v = askToast(req);
  if (v.kind !== "choices" || !v.choices.some((c) => c.id === choice)) return null;
  if (choice === "allow") return allow();
  if (choice === "deny") return deny(ASK_DENIED);
  if (choice === "always") return allowAlways(req);
  return null;
}

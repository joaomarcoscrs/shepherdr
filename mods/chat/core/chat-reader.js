// Following an agent's conversation from its own session log, as the window's chat does (mods/chat: find the
// log, read the newest page, then what it gains; earlier pages when asked; keep at most KEEP records):
// the service uses it for the web app. io.sh(script, args, machine) -> Promise<{ code, stdout }> runs a shell
// script on the agent's machine (null: here).
// Shared core: the source is core/chat-reader.js.

import { FIND, FIND_NATIVE_CURSOR, LOG, Conversation, records, resultText } from "./chat-log.js";
import { BgJobs } from "./bg-jobs.js";

export const PAGE = 200;
export const KEEP = 2000;
export const KINDS = { "claude-code": "claude", claude: "claude", codex: "codex", cursor: "cursor" };

const argv = (a) => Array.isArray(a) ? a : [];
const bridge = (a) => argv(a).some((w, i) => w === "cursor-chat" && i > 0 && /(?:^|\/)herdr-task$/.test(String(a[i - 1])));
const reportedKind = (t) => [t?.status?.app, t?.foreground?.name].map((k) => Object.hasOwn(KINDS, k) ? KINDS[k] : null).find(Boolean);
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;

function resumeRecord(t) {
  const label = t?.labels?.["herdr.resume"];
  if (typeof label !== "string" || label.length > 16384) return null;
  try { return JSON.parse(label); } catch { return null; }
}

// Provider recognition also covers a native worker still starting, so it can never get an ACP composer.
// Its actual transcript is accepted only after the foreground's store confirms the resume record.
export function nativeCursorTerminal(t) {
  const fg = t?.foreground, kind = reportedKind(t);
  if (bridge(fg?.argv)) return false;
  if (t?.status?.app === "cursor-agent" || fg?.name === "cursor-agent"
    || argv(fg?.argv).slice(0, 2).some((w) => /(?:^|\/)cursor-agent(?:\/|$)/.test(String(w)))) return true;
  if ((kind && kind !== "cursor") || bridge(t?.argv)) return false;
  const rec = resumeRecord(t);
  if (rec?.kind === "cursor" && !bridge(rec.argv) && (kind !== "cursor" || nativeLogId(rec.log))) return true;
  return kind !== "cursor" && (["cursor", "cursor-agent"].includes(t?.labels?.["herdr.agent"])
    || /(?:^|\/)cursor-agent$/.test(String(argv(t?.argv)[0])));
}

export const kindOf = (t) => nativeCursorTerminal(t) || bridge(t?.foreground?.argv) ? "cursor" : reportedKind(t) ?? null;
export const agentKindOf = (t) => kindOf(t) ?? (isCursor(t) ? "cursor" : null);
export function isCursor(t) {
  if (["cursor", "cursor-agent"].includes(t?.status?.app) || ["cursor-agent", "agent"].includes(t?.foreground?.name)) return true;
  return nativeCursorTerminal(t);
}

function nativeLogId(path) {
  if (typeof path !== "string" || !path.startsWith("/") || /[\x00-\x1f\x7f]/.test(path)) return null;
  if (path.slice(1).split("/").some((part) => !part || part === "." || part === "..")) return null;
  const match = /\/projects\/[^/]+\/agent-transcripts\/(?:([^/]+)\/)?([^/]+)\.jsonl$/.exec(path);
  return match && UUID.test(match[2]) && (!match[1] || match[1] === match[2]) ? match[2] : null;
}

// Older task records store provider + path. Recognize both native layouts and reject malformed native paths.
export function savedChatLog(path, kind, session) {
  if (typeof path !== "string" || !Object.hasOwn(KINDS, kind)) return null;
  if (kind === "cursor" && path.includes("/agent-transcripts/")) {
    const id = nativeLogId(path);
    return id && (session == null || id === session) ? { path, format: "cursor-native" } : null;
  }
  return { path, format: null };
}

export async function findChatLog(sh, t, kind) {
  const pid = t?.foreground?.pid;
  if (!kind || !Number.isSafeInteger(pid) || pid <= 0) return null;
  if (nativeCursorTerminal(t)) {
    if (t.state && t.state !== "live") return null;
    const rec = resumeRecord(t);
    if (rec?.kind !== "cursor" || typeof rec.session !== "string" || nativeLogId(rec.log) !== rec.session
      || (t.pid != null && rec.pid != null && rec.pid !== t.pid)) return null;
    const r = await sh(FIND_NATIVE_CURSOR, ["chat", String(pid), rec.session, rec.log]);
    return r.code === 0 && r.stdout?.trim() === rec.log ? rec.log : null;
  }
  const r = await sh(FIND, ["chat", kind, String(pid)]);
  return String(r.stdout ?? "").trim().split("\n").pop() || null;
}

export const chatLogArgs = (format, args) => ["log", ...(format === "cursor-native" ? [format] : []), ...args];

export class ChatReader {
  constructor(io, addr) {
    this.io = io;
    this.addr = addr;
    this.machine = addr.includes("/") ? addr.split("/")[0] : null;
    this.reset();
  }

  reset() {
    this.pinned = false;
    this.path = null;
    this.kind = null;
    this.format = null;
    this.pid = null;
    this.start = null;   // the first byte read (0: the log's very start)
    this.end = 0;
    this.records = [];
    this.conv = null;
    this.bg = new BgJobs(); // what it left running in the background
    this.state = "finding"; // finding | pending (no log yet) | ready | none (not a chat agent) | missing
  }

  log(args) { return this.io.sh(LOG, chatLogArgs(this.format, args), this.machine); }

  // An ended agent's own log (a finished task's last agent, as the Tasks panel opens it): read as it is, never
  // looked for again. -> this reader.
  /** @param {string} path @param {string} kind @param {string | null} session */
  fixed(path, kind, session = null) {
    this.reset();
    const log = savedChatLog(path, kind, session);
    this.path = log?.path ?? null;
    this.format = log?.format ?? null;
    this.kind = kind;
    this.pinned = true;
    if (!log) this.state = "missing";
    return this;
  }

  // Finds the log of the agent this terminal runs now (t: its herdr-core record). -> true when it changed.
  async locate(t) {
    if (this.pinned) return false;
    const kind = kindOf(t);
    const pid = t?.foreground?.pid;
    const format = nativeCursorTerminal(t) ? "cursor-native" : null;
    if (this.path && this.kind === kind && this.pid === pid && !format && !this.format) return false;
    const path = await findChatLog((script, args) => this.io.sh(script, args, this.machine), t, kind);
    if (path === "pending" || !path) {
      const was = this.state;
      this.reset();
      this.kind = kind;
      this.format = format;
      this.pid = pid;
      this.state = path === "pending" ? "pending" : "none";
      if (this.state === "pending") { this.conv = new Conversation(kind); this.start = 0; }
      return was !== this.state;
    }
    if (path === this.path && kind === this.kind && format === this.format) { this.pid = pid; return false; }
    this.reset();
    this.path = path;
    this.kind = kind;
    this.format = format;
    this.pid = pid;
    return true;
  }

  // ---- what it left running in the background (core/bg-jobs.js) ----

  run(script, args) { return this.io.sh(script, args, this.machine); }

  // A look at the agent's processes when one is due. -> true when what shows changed.
  async lookJobs() {
    if (this.pinned || !this.pid || !this.conv || !this.bg.due(this.kind, this.conv)) return false;
    return this.bg.look((s, a) => this.run(s, a), this.kind, this.pid, this.conv);
  }

  // Its shells and watchers now (none for a log read as it is).
  jobs() { return this.pinned || !this.conv ? [] : this.bg.list(this.kind, this.conv); }

  // One of them stopped. -> false when it's gone already.
  async stopJob(id) {
    const job = this.jobs().find((j) => j.id === id);
    return job ? this.bg.stop((s, a) => this.run(s, a), this.kind, this.pid, job) : false;
  }

  // What one of them printed last (Claude Code's), or null.
  async jobOutput(id) {
    const job = this.jobs().find((j) => j.id === id);
    return job ? this.bg.output((s, a) => this.run(s, a), job) : null;
  }

  rebuild() {
    this.conv = new Conversation(this.kind, this.format);
    this.conv.feed(this.records);
  }

  // The newest page first; after that, what the log gained since. -> true when there's something new.
  async pull() {
    if (!this.path) return false;
    const path = this.path;
    if (this.start === null) {
      const r = await this.log(["page", path, "999999999999999", String(PAGE)]);
      if (path !== this.path) return false;
      if (String(r.stdout).startsWith("missing")) { this.path = null; this.state = "missing"; return true; }
      const { head, records: recs } = records(r.stdout);
      if (head[0] !== "P") return false;
      this.start = Number(head[1]);
      this.end = Number(head[2]);
      this.records = recs;
      this.rebuild();
      this.state = "ready";
      return true;
    }
    const r = await this.log(["tail", path, String(this.end)]);
    if (path !== this.path) return false;
    if (String(r.stdout).startsWith("missing")) { this.path = null; this.state = "missing"; return true; }
    const { head, records: recs } = records(r.stdout);
    if (head[0] !== "T") return false;
    // Shorter than what was read: the file was replaced. Start over from its end.
    if (Number(head[2]) < this.end) { this.start = null; return this.pull(); }
    this.end = Number(head[3]);
    if (!recs.length) return false;
    this.records.push(...recs);
    if (this.records.length > KEEP) {
      // Down to three quarters, so the rebuild is now and then, not on every message.
      this.records = this.records.slice(-Math.floor(KEEP * 0.75));
      this.start = this.records[0].off;
      this.rebuild();
    } else this.conv.feed(recs);
    return true;
  }

  // What a tools row's calls printed, read from the log only when the row opens (as the window's chat
  // loadResults()): -> Map step id -> its first lines ("" when none).
  async results(steps) {
    const out = new Map();
    const want = (steps ?? []).filter((s) => s.out !== undefined);
    if (!want.length || !this.path) return out;
    const offs = [...new Set(want.map((s) => s.out))].slice(0, 40);
    const r = await this.log(["results", this.path, ...offs.map(String)]);
    for (const part of String(r.stdout ?? "").split("\x1e").slice(1)) {
      const nl = part.indexOf("\n");
      const off = Number(part.slice(0, nl));
      const line = part.slice(nl + 1).replace(/\n$/, "");
      for (const s of want) if (s.out === off) out.set(s.id, resultText(line, s.id) ?? "");
    }
    return out;
  }

  // The page before what is read. -> true when there was one.
  async earlier() {
    if (!this.start || !this.path) return false;
    const path = this.path, before = this.start;
    const r = await this.log(["page", path, String(before), String(PAGE)]);
    const { head, records: recs } = records(r.stdout);
    if (path !== this.path || this.start !== before || head[0] !== "P") return false;
    this.start = Number(head[1]);
    this.records = [...recs, ...this.records];
    this.rebuild();
    return true;
  }
}

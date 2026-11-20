// The model menu and restart from the web (2.1's restart sheet): what an agent runs on now and what its CLIs offer
// (core/agents.js, as the window's model menu); another model, level or Fast in place (core/live-switch.js, SHEP-181:
// the CLI's own commands, the same conversation); and a restart for what can't change in place (another CLI or
// permission mode): herdr-task switch for a task's agent, the Assistant's restart.sh for an Assistant, parked behind
// its chat.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { Hub } from "./hub.ts";
import { where, writable } from "./paths.ts";
import { codexAuto, sh } from "./model.ts";
import { layoutScript } from "./layout-scripts.ts";
import * as AG from "../core/agents.js";
import { chatLabel } from "../core/spaces.js";
import { agentKindOf as kindOf } from "../core/chat-reader.js";
import * as LS from "../core/live-switch.js";
import { historyLines } from "../core/mouse-leak.js";

const { DEFAULTS, parseDefaults, current, menuFor, rowFor, argvFor, sessionOf, NAMES, line, PICKS_FILE, parsePicks, pickKey, withPin, withRecent, isCursorChat, hasCodexAuto, selectableModel } = AG as any;
/** Whether Cursor may run this agent: anything but a reviewer or a helper (core/settings.js CURSOR_ROLES). */
export const cursorRole = (t: any) => !["reviewer", "helper"].includes(t?.labels?.["herdr.role"]);

async function run(cmd: string[]) {
  const p = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe", stdin: "ignore" });
  const [out, err] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text()]);
  return { code: await p.exited, out, err };
}

export class Agents {
  constructor(private hub: Hub) {}

  /** The person's pinned and recent models (core/agents.js PICKS_FILE), as the window's model menu keeps them here */
  private picksPath = where()["model-picks"];
  picks() {
    try { return parsePicks(readFileSync(this.picksPath, "utf8")); } catch { return parsePicks(""); }
  }
  private savePicks(p: unknown) {
    mkdirSync(dirname(writable(this.picksPath)), { recursive: true });
    writeFileSync(this.picksPath, JSON.stringify(p, null, 2));
  }
  /** the star on a model's row: pinned to the top of its agent's list, or not any more */
  pin(agent: string, model: string) {
    const p = withPin(this.picks(), pickKey(agent, model));
    this.savePicks(p);
    return p;
  }

  private async defaults(kind: string, machine: string | null) {
    const r = await sh(this.hub, DEFAULTS, ["defaults", kind], machine).catch(() => null);
    return parseDefaults(kind, r?.stdout ?? "");
  }

  /** A CLI's own settings on a machine as last read (the chat's model button reads them on every change, as the
   *  window's chat keeps them: this.dflt), read again in the background at most once a minute. */
  private known = new Map<string, { at: number; value: any }>();
  cached(kind: string, machine: string | null) {
    const k = `${machine ?? ""}|${kind}`, was = this.known.get(k);
    if (!was || Date.now() - was.at > 60_000) {
      this.known.set(k, { at: Date.now(), value: was?.value ?? null });
      void this.defaults(kind, machine).then((value) => this.known.set(k, { at: Date.now(), value }), () => undefined);
    }
    return was?.value ?? null;
  }

  /** The window's model menu (mods/chat openPicker → mods/chat-models) for the agent at addr: what it runs on, each
   *  installed CLI's models, levels, Fast and permission modes, the person's pinned and recent ones; offline: the
   *  machine it runs on when that machine can't be reached now (a restart there waits for it) */
  async menu(addr: string, reader: any) {
    const t: any = this.hub.terminals.get(addr);
    if (!t) throw new Error("That agent isn't here any more.");
    const machine = addr.includes("/") ? addr.split("/")[0] : null;
    const kind = kindOf(t) ?? reader?.kind ?? "claude";
    const [claude, codex, cursor, have] = await Promise.all([this.defaults("claude", machine), this.defaults("codex", machine), this.defaults("cursor", machine),
      sh(this.hub, 'for c in "$@"; do command -v "$c" >/dev/null 2>&1 && echo "$c"; done', ["agents", "claude", "codex"], machine).then((r: any) => new Set(String(r?.stdout ?? "").split("\n").filter(Boolean)), () => null)]);
    const dflts: any = { claude, codex, cursor };
    // Cursor only where its CLI is (cursor-agent or agent on that machine's PATH), and never for a reviewer or a helper
    // (core/settings.js forRole): a task's worker runs Cursor's own UI, an Assistant its chat bridge (SHEP-6)
    const offered = (k: string) => k === kind || (k === "cursor" ? !!cursor?.installed && cursorRole(t) : !have || have.has(k));
    let meta = reader?.conv?.meta ?? {};
    // a resumed conversation's log names the model it ran on before: until this process writes, its arguments say
    if (t.created_ms && meta.at && meta.at < t.created_ms) meta = { used: meta.used, window: meta.window };
    const c = current(kind, t.foreground?.argv ?? t.argv ?? [], meta, dflts[kind]);
    const row = rowFor(kind, c);
    // not a task's agent (an Assistant): Cursor runs behind its chat bridge there, which has no Auto (CURSOR_CHAT_PERMS)
    const chat = !t.labels?.["herdr.task"];
    const menus = Object.fromEntries(["claude", "codex", "cursor"].filter(offered).map((k) => [k, menuFor(k, dflts[k], k === kind ? row : null, { chat })]));
    const offline = machine && !(await this.reachable(machine)) ? machine : null;
    const codexAutoUpgrade = !offline && kind === "codex" && c.perm === "auto" && !hasCodexAuto(t.foreground?.argv ?? t.argv)
      && await codexAuto(this.hub, machine);
    return { current: { agent: kind, model: row, effort: c.effort ?? null, fast: !!c.fast, perm: c.perm ?? null }, menus,
      permDefault: { claude: claude?.mode ?? "manual", codex: "auto", cursor: "auto" }, picks: this.picks(), offline, codexAutoUpgrade };
  }

  /** whether another machine answers now (hc machine ls: connected) */
  private async reachable(machine: string) {
    const r = await run([process.env.SHEPHERDR_HC || "hc", "machine", "ls", "--json"]).catch(() => null);
    try { return (JSON.parse(r?.out ?? "{}").machines ?? []).some((m: any) => m.name === machine.split("/")[0] && m.state === "connected"); } catch { return true; }
  }

  /** Each CLI's own settings here (Settings › Agents' model menu: core/settings.js choicesFrom). */
  async ownDefaults() {
    const [claude, codex, cursor] = await Promise.all([this.defaults("claude", null), this.defaults("codex", null), this.defaults("cursor", null)]);
    return { claude, codex, cursor };
  }

  /** What an unset role runs, in the window's words ("Opus 5.5 · 1M · High"): each CLI's own defaults here. */
  async defaultLines(): Promise<Record<string, string>> {
    const out: Record<string, string> = {};
    for (const k of ["claude", "codex", "cursor"]) {
      const d: any = await this.defaults(k, null).catch(() => null);
      if (k === "cursor" && !d?.installed) continue;
      if (d) out[k] = line({ agent: k, model: d.model, effort: d.effort ?? d.per?.[String(d.model ?? "").replace(/\[1m\]$/, "")] ?? null, fast: d.fast }).split(" · ").slice(1).join(" · ");
    }
    return out;
  }

  /** What each CLI offers on a machine (null: here), as the restart sheet lists them. */
  async choices(machine: string | null) {
    const [claude, codex, cursor] = await Promise.all([this.defaults("claude", machine), this.defaults("codex", machine), this.defaults("cursor", machine)]);
    const dflt: any = { claude, codex, cursor };
    return ["claude", "codex", ...(cursor?.installed ? ["cursor"] : [])].map((k) => {
      const m = menuFor(k, dflt[k], null);
      // its permission modes, and the one a new agent gets unless picked (the CLI's own: Claude Code's settings mode)
      return { kind: k, name: NAMES[k], models: m.models.map((x: any) => ({ id: x.id, label: x.name, efforts: x.efforts, ...(x.defaultEffort ? { defaultEffort: x.defaultEffort } : {}) })),
        speeds: m.models.some((x: any) => x.fast) ? ["normal", "fast"] : ["normal"], perms: m.perms ?? [],
        permDefault: k === "claude" ? dflt.claude?.mode ?? "manual" : "auto" };
    });
  }

  /** What the restart sheet shows for the agent at addr (log: its reader, for the session and what the log says). */
  async info(addr: string, reader: any) {
    const t: any = this.hub.terminals.get(addr);
    if (!t) throw new Error("That agent isn't here any more.");
    const machine = addr.includes("/") ? addr.split("/")[0] : null;
    const kind = kindOf(t) ?? "claude";
    const [claude, codex, cursor] = await Promise.all([this.defaults("claude", machine), this.defaults("codex", machine), this.defaults("cursor", machine)]);
    const dflt: any = { claude, codex, cursor };
    const c = current(kind, t.foreground?.argv ?? t.argv ?? [], reader?.conv?.meta ?? {}, dflt[kind]);
    const clis = ["claude", "codex", ...((cursor?.installed && cursorRole(t)) || kind === "cursor" ? ["cursor"] : [])].map((k) => {
      const m = menuFor(k, dflt[k], k === kind ? c.model : null);
      return { kind: k, name: NAMES[k], models: m.models.map((x: any) => ({ id: x.id, label: x.name, efforts: x.efforts, ...(x.defaultEffort ? { defaultEffort: x.defaultEffort } : {}) })),
        speeds: m.models.some((x: any) => x.fast) ? ["normal", "fast"] : ["normal"] };
    });
    const on = { kind, model: rowFor(kind, c) ?? undefined, effort: c.effort ?? undefined, speed: c.fast ? "fast" : "normal" };
    return { role: t.labels?.["herdr.task"] ? "task" : "coordinator", on, onKnown: true, onWords: line(c), want: on, clis, working: t.status?.state === "working" };
  }

  /** The model menu's pick: in place when only the model, level or Fast changes (the same agent, its conversation
   *  on), else a restart. Resolves with { live } or { terminal } (the restarted agent's address). */
  async change(addr: string, reader: any, pick: { kind: string; model?: string; effort?: string; speed?: string; perm?: string }) {
    if (!selectableModel(pick.kind, pick.model)) throw new Error("Pick a model for the conversation.");
    const t: any = this.hub.terminals.get(addr);
    if (!t || t.state !== "live") throw new Error("That agent isn't running.");
    const machine = addr.includes("/") ? addr.split("/")[0] : null;
    const kind = kindOf(t) ?? reader?.kind ?? "claude";
    const dflt = await this.defaults(kind, machine);
    let meta = reader?.conv?.meta ?? {};
    if (t.created_ms && meta.at && meta.at < t.created_ms) meta = { used: meta.used, window: meta.window };
    const c = current(kind, t.foreground?.argv ?? t.argv ?? [], meta, dflt);
    const from = { agent: kind, model: rowFor(kind, c) ?? "", effort: c.effort ?? "", fast: !!c.fast, perm: c.perm ?? "",
      chat: isCursorChat(t.foreground?.argv ?? t.argv ?? []) };
    const to = { agent: pick.kind, model: pick.model ?? "", effort: pick.effort ?? "", fast: pick.speed === "fast", perm: pick.perm ?? "" };
    if (!LS.liveOK(from, to)) return { terminal: await this.restart(addr, reader, pick) };
    const state = t.status?.state;
    if (state === "working") throw new Error("It's busy with a turn. Switch once it's done, or stop it first.");
    if (state === "blocked") throw new Error("It's waiting on a question. Answer it, then switch.");
    if (to.agent === "codex" && to.perm === "auto" && !hasCodexAuto(t.foreground?.argv ?? t.argv) &&
        await codexAuto(this.hub, machine)) {
      return { terminal: await this.restart(addr, reader, pick, true) };
    }
    const hc = (args: string[]) => run([process.env.SHEPHERDR_HC || "hc", ...args]);
    // a send returns once a turn starts or ~5 s pass, and a command starts none: its answer on screen is what counts,
    // so each is waited for only as long as a refusal takes (it comes at once), not for the rest
    const io = {
      send: async (text: string) => {
        const p = this.hub.send(addr, text);
        const r = await Promise.race([p, Bun.sleep(800).then(() => null)]);
        if (r && r.code !== 0) throw new Error("It couldn't be reached to switch.");
      },
      keys: async (...keys: string[]) => { await hc(["keys", addr, ...keys]); },
      // Codex: its screen alone (reading its history scrolls it with mouse reports, SHEP-14)
      screen: async () => (await hc(["read", addr, "--lines", String(historyLines(kind, 2000))])).out,
      readFile: async (rel: string) => {
        const r = await sh(this.hub, 'cat "$HOME/$1"', ["read", rel], machine);
        if (r.code !== 0) throw new Error("no file");
        return r.stdout;
      },
      writeFile: async (rel: string, text: string) => {
        await sh(this.hub, 'printf %s "$2" > "$HOME/$1"', ["write", rel, text], machine);
      },
      sleep: (ms: number) => Bun.sleep(ms),
    };
    const names = Object.fromEntries(menuFor(kind, dflt, from.model || null).models.map((m: any) => [m.id, m.name]));
    await LS.switchLive(io, kind, from, to, { names });
    this.known.delete(`${machine ?? ""}|${kind}`);
    this.used(pick);
    return { live: true };
  }

  /** Restarts it on the picked CLI, model, effort and speed; resolves with the new agent's address. */
  async restart(addr: string, reader: any, pick: { kind: string; model?: string; effort?: string; speed?: string; perm?: string }, supported?: boolean) {
    const t: any = this.hub.terminals.get(addr);
    if (!t || t.state !== "live") throw new Error("That agent isn't running.");
    const kind = kindOf(t) ?? "claude";
    const machine = addr.includes("/") ? addr.split("/")[0] : null;
    const old = { agent: kind, argv: t.foreground?.argv ?? t.argv ?? [], session: sessionOf(kind, reader?.path ?? null) };
    const task = t.labels?.["herdr.task"];
    // Assistants use Cursor's chat bridge. Task workers keep Cursor's native terminal and its permissions.
    const built = argvFor({ agent: pick.kind, model: pick.model, effort: pick.effort, fast: pick.speed === "fast", perm: pick.perm ?? current(kind, old.argv, {}, {}).perm }, old,
      { chat: !task, codexAuto: pick.kind === "codex" && (supported ?? await codexAuto(this.hub, machine)) });
    const pane = t.labels?.["herdr.chat"] ?? "";
    if (task) {
      const r = await run(["herdr-task", ...(machine ? ["--on", machine] : []), "switch", task, "--folder", t.foreground?.cwd ?? t.cwd, "--argv", JSON.stringify(built.base), "--json"]);
      let out: any = {};
      try { out = JSON.parse(r.out || "{}"); } catch {}
      if (r.code !== 0 || !out.terminal) throw new Error((r.err || r.out).trim() || "herdr-task switch failed");
      const next = machine && !out.terminal.includes("/") ? `${machine}/${out.terminal}` : out.terminal;
      if (pane) await run([process.env.SHEPHERDR_HC || "hc", "label", next, `herdr.chat=${chatLabel(pane)}`, `herdr.chat.origin=${t.labels?.["herdr.chat.origin"] || addr}`]);
      await run(["herdr-app", "do", "terminal.park", "--terminal", next]);
      this.used(pick);
      return next as string;
    }
    const script = layoutScript("restart.sh");
    if (!script) throw new Error("This install can't restart an Assistant: update Shepherdr on that machine.");
    const p = Bun.spawn(["bash", script, addr, JSON.stringify(built.argv)], { stdout: "pipe", stderr: "pipe", stdin: "ignore",
      env: { ...process.env, HERDR_RESTART_CHAT: pane, ...(built.resume ? { HERDR_RESTART_RESUME: "1" } : {}) } });
    const [out, err] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text()]);
    const next = out.trim().split("\n").pop();
    if ((await p.exited) !== 0 || !next) throw new Error(err.trim() || "The agent didn't restart.");
    this.used(pick);
    return next;
  }

  /** restarted on a model: first among the recent ones */
  private used(pick: { kind: string; model?: string }) {
    if (pick.model) try { this.savePicks(withRecent(this.picks(), pickKey(pick.kind, pick.model))); } catch {}
  }
}

// One permission, one toast and one sound (SHEP-19): an agent's chat (mods/chat) tells its Needs you (its status went
// blocked) and its requests (askAlert) apart, whichever comes first, and two requests at once stay two toasts. The
// chat's methods run on a stand-in chat; the toasts go through lib/ui.js toast() into a stand-in shared state.
import { beforeEach, describe, expect, mock, test } from "bun:test";
import { fakeHerdr } from "./fake-herdr";
import { fakeGpuiBase, fakeGpuiKit } from "./fake-gpui";

const shared = new Map<string, any>();
const sounds: string[] = [];
const term = { address: "t7", state: "live", status: "blocked", labels: { "herdr.role": "coordinator", "herdr.space": "s1" }, report: { msg: "approve Bash: gh pr list" } };
mock.module("herdr", () => fakeHerdr({
  shared: (k: string) => shared.get(k), setShared: (k: string, v: unknown) => { shared.set(k, v); },
  playSound: (s: string) => { sounds.push(s); }, settings: () => ({}), focused: () => null, window: () => ({ active: false }),
  terminals: () => [term], agents: () => [], panes: () => [{ pane: "p1", tab: "tab1", mod: "chat" }],
  spaces: () => [{ id: "s1", name: "inference", tabs: [{ id: "tab1" }] }], machines: () => [],
  layout: () => ({ floating: [{ content: { mod: "toasts" } }] }), exec: async () => ({ code: 1, stdout: "", stderr: "" }),
  hc: async () => ({ code: 1, stdout: "", stderr: "" }),
}));
mock.module("gpui-kit", () => fakeGpuiKit());
mock.module("gpui-base", () => fakeGpuiBase());
(globalThis as any).localStorage ??= { getItem: () => null, setItem: () => {}, removeItem: () => {} };

const { default: Chat } = await import("../mods/chat/main.js");
const { words } = await import("../core/asks.js");

const ask = (id: string, command: string) => { const req = { tool_name: "Bash", tool_input: { command } }; return { id, req, words: words(req) }; };
let chat: any, pending: Promise<unknown>[], onRead: () => void;
const cx: any = new Proxy({
  timer: { after: (_ms: number, f: any) => f(cx), every: () => {} },
  spawn: (f: any) => { const p = Promise.resolve(f(cx)); pending.push(p); return p; },
}, { get: (t, k) => (t as any)[k] ?? (() => {}) });
const settle = async () => { for (let i = 0; i < 5; i++) await Promise.all(pending.splice(0)); };
const toasts = () => (shared.get("toasts") ?? []).map((n: any) => [n.key, n.body]);

describe("one permission, one toast", () => {
  beforeEach(() => {
    shared.clear(); sounds.length = 0; pending = []; onRead = () => {};
    chat = Object.create(Chat.prototype);
    Object.assign(chat, { addr: "t7", asks: [], thread: null, machine: null });
    chat.myPane = () => "p1";
    chat.status = () => "blocked";
    // the request's file, read when the chat reads its asks (the loop's read, or the one before a Needs you)
    chat.readAsks = async () => { onRead(); };
  });

  test("status first, request read right after: only the request's toast, one sound", async () => {
    onRead = () => { const a = ask("t7.100.42", "gh pr list"); chat.asks = [a]; chat.askAlert([a]); };
    chat.needAlert(cx);
    await settle();
    expect(toasts()).toEqual([["ask:t7.100.42", "Run: gh pr list"]]);
    expect(sounds).toEqual(["blocked"]);
  });

  test("status first, its request's file a moment later: the request's toast takes the Needs you's place, no second sound", async () => {
    chat.needAlert(cx);
    await settle();
    expect(toasts()).toEqual([["need:t7", "approve Bash: gh pr list"]]);
    const a = ask("t7.100.42", "gh pr list");
    chat.asks = [a];
    chat.askAlert([a]);
    expect(toasts()).toEqual([["ask:t7.100.42", "Run: gh pr list"]]);
    expect(sounds).toEqual(["blocked"]);
  });

  test("request first, then the status: nothing more", async () => {
    const a = ask("t7.100.42", "gh pr list");
    chat.asks = [a];
    chat.askAlert([a]);
    chat.needAlert(cx);
    await settle();
    expect(toasts()).toEqual([["ask:t7.100.42", "Run: gh pr list"]]);
    expect(sounds).toEqual(["blocked"]);
    expect(shared.get("toasts")[0].ask).toEqual({ id: "t7.100.42" });
    expect(shared.get("toasts")[0].go).toEqual({ tab: "tab1", pane: "p1" });
  });

  test("two requests at once from one agent: two toasts, each its own, one sound", () => {
    const a = ask("t7.100.42", "gh pr list"), b = ask("t7.101.42", "curl -s https://example.com");
    chat.asks = [a, b];
    chat.askAlert([a, b]);
    expect(toasts()).toEqual([["ask:t7.100.42", "Run: gh pr list"], ["ask:t7.101.42", "Run: curl -s https://example.com"]]);
    expect(sounds).toEqual(["blocked"]);
  });

  test("blocked with no request (a question in words): its Needs you, as before", async () => {
    chat.needAlert(cx);
    await settle();
    expect(toasts()).toEqual([["need:t7", "approve Bash: gh pr list"]]);
    expect(shared.get("toasts")[0].ask).toBeUndefined();
    expect(sounds).toEqual(["blocked"]);
  });

  test("its space's Notify off: no toast, no sound", async () => {
    shared.set("notify", { spaces: { s1: "Off" } });
    const a = ask("t7.100.42", "gh pr list");
    chat.askAlert([a]);
    expect(toasts()).toEqual([]);
    expect(sounds).toEqual([]);
  });
});

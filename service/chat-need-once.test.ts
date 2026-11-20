// One permission, one toast and one sound (SHEP-19): an agent's chat (mods/chat) tells its Needs you (its status went
// blocked) and its requests (askAlert) apart, whichever comes first, and two requests at once stay two toasts. The
// chat's methods run on a stand-in chat; the toasts go through lib/ui.js toast() into a stand-in shared state.
import { beforeEach, describe, expect, mock, test } from "bun:test";
import { fakeHerdr } from "./fake-herdr";
import { fakeGpuiBase, fakeGpuiKit } from "./fake-gpui";

const shared = new Map<string, any>();
const sounds: string[] = [];
const ran: [string, unknown][] = [];
let loginWorker = false;
const term = { address: "t7", created_ms: 70, state: "live", status: "blocked", labels: { "herdr.role": "coordinator", "herdr.space": "s1" }, report: { msg: "approve Bash: gh pr list" } };
mock.module("herdr", () => fakeHerdr({
  shared: (k: string) => shared.get(k), setShared: (k: string, v: unknown) => { shared.set(k, v); },
  playSound: (s: string) => { sounds.push(s); }, settings: () => ({}), focused: () => null, window: () => ({ active: false, platform: "macos" }),
  terminals: () => [loginWorker ? { ...term, address: "t8", labels: { "herdr.task": "shop#4" } } : term], agents: () => [], panes: () => [{ pane: "p1", tab: "tab1", mod: "chat" }],
  spaces: () => [{ id: "s1", name: "inference", tabs: [{ id: "tab1" }] }], machines: () => [],
  run: (cmd: string, args: unknown) => { ran.push([cmd, args]); },
  layout: () => ({ children: [{ id: "tasks-thread-body" }], floating: [{ content: { mod: "toasts" } }] }), exec: async (cmd: string, args: string[]) => {
    ran.push([cmd, args]);
    return { code: 0, stdout: cmd === "sh" ? args.at(-1) + "\n" : "", stderr: "" };
  },
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

describe("a task's worker shown in a thread", () => {
  test("its request gets its toast there (the Inbox leaves a shown thread to it), once across its chats", () => {
    shared.clear(); sounds.length = 0;
    const thread: any = Object.create(Chat.prototype);
    Object.assign(thread, { addr: "t7", asks: [], thread: { terminal: "t7" }, machine: null });
    thread.myPane = () => "p1";
    const a = ask("t7.100.42", "npm test");
    thread.askAlert([a]);
    // its own tab's chat reads the same request: no second toast, no second sound
    const own: any = Object.create(Chat.prototype);
    Object.assign(own, { addr: "t7", asks: [], thread: null, machine: null });
    own.myPane = () => "p1";
    own.askAlert([a]);
    expect(toasts()).toEqual([["ask:t7.100.42", "Run: npm test"]]);
    expect(sounds).toEqual(["blocked"]);
  });
});


test("a sign-in toast goes to the chat and only the card click opens Chrome", async () => {
  shared.clear(); ran.length = 0; pending = [];
  shared.set("addons.off", ["browser"]);
  const c: any = Object.create(Chat.prototype);
  Object.assign(c, { addr: "t7", machine: null, askDir: "/fixture", myPane: () => "p1" });
  const req = { tool_name: "HerdrLogin", owner: { created: 70, task: null },
    tool_input: { kind: "site", origin: "https://example.invalid" } };
  const a = { id: `t7.login.${Math.floor(Date.now() / 1000)}.42`, req, words: words(req) };
  c.askAlert([a]);
  expect(shared.get("toasts")[0]).toMatchObject({ go: { tab: "tab1", pane: "p1" }, body: "Sign-in needed" });
  expect(ran).toEqual([]);
  c.openLogin(a, cx);
  await settle();
  expect(ran).toContainEqual(["open", ["-a", "Google Chrome", "https://example.invalid"]]);

  ran.length = 0;
  c.addr = "t8";
  c.openLogin(a, cx);
  await settle();
  expect(ran).toEqual([]);
});

test("the legacy Browser action points its panel at exactly the worker on the card", async () => {
  loginWorker = true;
  try {
    shared.clear(); ran.length = 0; pending = [];
    const c: any = Object.create(Chat.prototype);
    Object.assign(c, { addr: "t7", machine: null, askDir: "/fixture" });
    const req = { tool_name: "HerdrLogin", owner: { created: 70, task: "shop#4" }, tool_input: { kind: "browser" } };
    c.openLogin({ id: `t8.login.${Math.floor(Date.now() / 1000)}.42`, terminal: "t8", ref: "SHOP-4", req, words: words(req) }, cx);
    await settle();
    expect(shared.get("tasks.thread")).toMatchObject({ terminal: "t8", task: "SHOP-4" });
    expect(ran).toContainEqual(["layout.select", { stack: "tasks-thread-body", child: "tasks-thread-browser" }]);
  } finally { loginWorker = false; }
});

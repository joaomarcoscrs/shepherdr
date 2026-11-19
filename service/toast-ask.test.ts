// A permission's toast (SHEP-19, mods/toasts): its card's choices on the toast, answered only by their own buttons,
// with the request read again just before and written once; anything that can't be shown whole is Review. No window
// runs here: gpui and herdr are stand-ins, and the answer writer's shell is recorded, never run (nothing is approved).
import { beforeEach, describe, expect, mock, test } from "bun:test";
import { fakeHerdr } from "./fake-herdr";
import { built, byId, dispatch, fakeGpuiBase, fakeGpuiKit } from "./fake-gpui";
import { allow, deny, allowAlways } from "../core/asks.js";
import { ASK_ARM_MS, ASK_DENIED } from "../core/toasts.js";

const shared = new Map<string, any>();
const files = new Map<string, string>();
const runs: [string, any][] = [];
let execs: string[][] = [];
// what the answer writer's shell says: 0 sent, 3 no longer waiting, 4 answered already
let answerCode = 0;
let waiting: Set<string> | null = null;
let hovered = false;
const space = { id: "s1", active: false, tabs: [{ id: "tab1" }] };
const exec = async (_cmd: string, args: string[]) => {
  execs.push(args);
  if (String(args[1]).includes("ln ")) return { code: answerCode, stdout: "", stderr: "" };
  return { code: 0, stdout: [...(waiting ?? [])].join("\n"), stderr: "" };
};
const timers: ((cx: any) => void)[] = [];
const spawned: Promise<unknown>[] = [];
const cx: any = new Proxy({
  timer: { every: (_ms: number, f: any) => timers.push(f), after: (_ms: number, f: any) => f(cx) },
  spawn: (f: any) => { const p = Promise.resolve(f(cx)); spawned.push(p); return p; },
  notify: () => {},
}, { get: (t, k) => (t as any)[k] ?? (() => {}) });
const settle = async () => { for (let i = 0; i < 5; i++) await Promise.all(spawned.splice(0)); };

mock.module("herdr", () => fakeHerdr({
  shared: (k: string) => shared.get(k), setShared: (k: string, v: unknown) => { shared.set(k, v); },
  readTextFile: async (p: string) => files.get(p) ?? null, exec, appHome: () => "/app",
  run: (name: string, args: any) => { runs.push([name, args]); }, placement: () => ({ hovered }),
  theme: () => new Proxy({}, { get: () => "#000000" }), settings: () => ({}), spaces: () => [space],
  panes: () => [{ pane: "p1", tab: "tab1" }], layout: () => ({ floating: [{ content: { mod: "toasts" } }] }),
  terminals: () => [], agents: () => [],
}));
mock.module("gpui-kit", () => fakeGpuiKit());
mock.module("gpui-base", () => fakeGpuiBase());
(globalThis as any).localStorage ??= { getItem: () => null, setItem: () => {}, removeItem: () => {} };

const { default: Toasts } = await import("../mods/toasts/main.js");

const ID = "t7.1763560000.4242";
const REQ = `/app/data/chat-asks/req/${ID}.json`;
const bashReq = (command: string, extra = {}) => JSON.stringify({ tool_name: "Bash", tool_input: { command }, herdr_agent: "cursor", ...extra });
const always = { option: "allow-always-7", name: "Allow always", scope: "adds Shell(gh) to Cursor's allowlist, for every such command" };
const askToastN = (over: any = {}) => ({ id: "n1", at: Date.now(), level: "alert", title: "inference Assistant · Needs you", body: "Run: gh pr list",
  key: `ask:${ID}`, covers: ["need:t7"], terminal: "t7", ask: { id: ID }, go: { tab: "tab1", pane: "p1" }, ...over });

let view: any;
async function show(...list: any[]) {
  shared.set("toasts", list);
  built.length = 0;
  view.render(cx);       // reads the request
  await settle();
  built.length = 0;
  view.render(cx);       // draws what it read
}
const answers = () => execs.filter((a) => String(a[1]).includes("ln ")).map((a) => ({ id: a[4], how: a[5], text: a[6] }));
const press = (id: string) => { const el = byId(id); if (!el) throw new Error(`no ${id}`); return dispatch(el, "on_click", cx); };
const arm = () => { for (const s of view.asks.values()) s.shownAt = Date.now() - ASK_ARM_MS - 1; };

describe("a permission's toast", () => {
  beforeEach(() => {
    shared.clear(); files.clear(); runs.length = 0; execs = []; answerCode = 0; waiting = null; hovered = false; timers.length = 0;
    view = new Toasts();
    view.init({}, cx);
  });

  test("shows its card's choices and the exact command; Allow sends the card's own answer once, never the toast's open", async () => {
    files.set(REQ, bashReq('gh pr view 3358 --repo roboflow/inference --comments 2>&1 | rg -A5 "dkosowski87" | head -20'));
    await show(askToastN());
    expect(byId("toast-allow-n1")).toBeDefined();
    expect(byId("toast-deny-n1")).toBeDefined();
    expect(byId("toast-always-n1")).toBeUndefined();
    const lines = built.filter((e) => e.calls.some(([n, a]: any) => n === "id" && String(a[0]).startsWith("toast-exact-n1-")))
      .map((e) => e.kids.join(""));
    expect(lines.join("")).toBe('gh pr view 3358 --repo roboflow/inference --comments 2>&1 | rg -A5 "dkosowski87" | head -20');
    // just shown: not armed yet, a click does nothing
    press("toast-allow-n1");
    await settle();
    expect(answers()).toEqual([]);
    arm();
    const ran = press("toast-allow-n1");
    // the button's own handler ran and stopped there: the toast's body (open the chat) never ran
    expect(ran.length).toBe(1);
    expect(runs).toEqual([]);
    press("toast-allow-n1");          // a quick second press while it goes
    await settle();
    expect(answers()).toEqual([{ id: ID, how: "Allowed from a notification", text: allow() }]);
    expect(runs).toEqual([]);
    // said in place; the toast goes once the pointer leaves, never answering anything else
    built.length = 0; view.render(cx);
    expect(byId("toast-allow-n1")).toBeUndefined();
    timers.forEach((f) => f(cx));
    expect(shared.get("toasts")).toEqual([]);
  });

  test("Deny is Cursor's reject with the toast's words; Allow always picks the exact option its request offered", async () => {
    files.set(REQ, bashReq("gh pr list", { cursor_always: always }));
    await show(askToastN());
    expect(byId("toast-allow-n1").kids).toContain("Allow once");
    expect(byId("toast-always-n1")).toBeDefined();
    arm();
    press("toast-always-n1");
    await settle();
    expect(answers()).toEqual([{ id: ID, how: "Allowed always from a notification", text: allowAlways(JSON.parse(files.get(REQ)!)) }]);
    // a second toast, another request of the same agent: its own answer, under its own id
    const ID2 = "t7.1763560001.4242";
    files.set(`/app/data/chat-asks/req/${ID2}.json`, bashReq("gh pr diff 1"));
    await show(askToastN(), askToastN({ id: "n2", key: `ask:${ID2}`, ask: { id: ID2 } }));
    arm();
    press("toast-deny-n2");
    await settle();
    expect(answers()[1]).toEqual({ id: ID2, how: "Denied from a notification", text: deny(ASK_DENIED) });
  });

  test("the request read again: changed, gone or answered elsewhere sends nothing, and says so", async () => {
    files.set(REQ, bashReq("gh pr list", { cursor_always: always }));
    await show(askToastN());
    arm();
    files.set(REQ, bashReq("gh pr list"));     // no longer offers Allow always
    press("toast-always-n1");
    await settle();
    expect(answers()).toEqual([]);
    built.length = 0; view.render(cx);
    expect(built.some((e) => e.kids.includes("It changed: review it in its chat"))).toBe(true);

    view = new Toasts(); view.init({}, cx);
    files.set(REQ, bashReq("gh pr list"));
    await show(askToastN());
    arm();
    files.delete(REQ);                          // answered in the terminal, or the agent ended
    press("toast-allow-n1");
    await settle();
    expect(answers()).toEqual([]);

    view = new Toasts(); view.init({}, cx);
    files.set(REQ, bashReq("gh pr list"));
    await show(askToastN());
    arm();
    answerCode = 4;                             // the chat's card answered first
    press("toast-allow-n1");
    await settle();
    built.length = 0; view.render(cx);
    expect(built.some((e) => e.kids.includes("Answered already"))).toBe(true);
    expect(byId("toast-allow-n1")).toBeUndefined();
  });

  test("answered elsewhere while it shows: no choices left, gone once the pointer leaves", async () => {
    files.set(REQ, bashReq("gh pr list"));
    await show(askToastN());
    waiting = new Set();
    hovered = true;
    timers.forEach((f) => f(cx));
    await settle();
    built.length = 0; view.render(cx);
    expect(byId("toast-allow-n1")).toBeUndefined();
    expect(built.some((e) => e.kids.includes("No longer waiting"))).toBe(true);
    expect(shared.get("toasts").length).toBe(1);
    hovered = false;
    timers.forEach((f) => f(cx));
    expect(shared.get("toasts")).toEqual([]);
    expect(answers()).toEqual([]);
  });

  test("too long, a plan, another machine's: Review opens its card, nothing answered", async () => {
    files.set(REQ, bashReq("x".repeat(400)));
    await show(askToastN());
    expect(byId("toast-allow-n1")).toBeUndefined();
    const ran = press("toast-open-n1");
    expect(ran.length).toBe(1);
    expect(runs).toEqual([["space.select", { id: "s1" }], ["tab.select", { id: "tab1" }], ["pane.focus", { pane: "p1" }]]);
    expect(answers()).toEqual([]);

    runs.length = 0;
    view = new Toasts(); view.init({}, cx);
    await show(askToastN({ ask: { id: "t3.1.9", machine: "vm" }, key: "ask:vm/t3.1.9" }));
    expect(byId("toast-allow-n1")).toBeUndefined();
    expect(byId("toast-open-n1").kids).toContain("Review");
    expect(execs).toEqual([]);                 // another machine's request isn't read from here
  });

  test("the body, the X, the timeout and the pointer never answer", async () => {
    files.set(REQ, bashReq("gh pr list"));
    await show(askToastN());
    arm();
    dispatch(byId("toast-n1"), "on_click", cx);  // the toast's body: its chat
    expect(runs.map((r) => r[0])).toEqual(["space.select", "tab.select", "pane.focus"]);
    await show(askToastN());
    dispatch(byId("toast-x-n1") ?? byId("toast-n1"), "on_click", cx);
    shared.set("toasts", [askToastN({ at: Date.now() - 60000 })]);
    timers.forEach((f) => f(cx));
    await settle();
    expect(shared.get("toasts")).toEqual([]);
    expect(answers()).toEqual([]);
  });

  test("while the pointer is on them nothing moves: a new toast waits", async () => {
    files.set(REQ, bashReq("gh pr list"));
    await show(askToastN({ id: "a" }), askToastN({ id: "b", key: "x" }), askToastN({ id: "c", key: "y" }));
    hovered = true;
    shared.set("toasts", [...shared.get("toasts"), { id: "d", at: Date.now(), title: "new" }]);
    built.length = 0; view.render(cx);
    expect(byId("toast-d")).toBeUndefined();
    expect(byId("toast-a")).toBeDefined();
    hovered = false;
    built.length = 0; view.render(cx);
    expect(byId("toast-d")).toBeDefined();
    expect(byId("toast-a")).toBeUndefined();
  });

  test("another Needs you of an agent: Open chat, and a plain toast as before", async () => {
    await show({ id: "n9", at: Date.now(), level: "alert", title: "a · Needs you", body: "Tea or coffee?", key: "need:t7", terminal: "t7", go: { tab: "tab1" } },
      { id: "e1", at: Date.now(), level: "error", title: "Artifact didn't open", body: "Nope." });
    expect(byId("toast-open-n9").kids).toContain("Open chat");
    expect(byId("toast-open-e1")).toBeUndefined();
    press("toast-open-n9");
    expect(runs).toEqual([["space.select", { id: "s1" }], ["tab.select", { id: "tab1" }]]);
  });
});

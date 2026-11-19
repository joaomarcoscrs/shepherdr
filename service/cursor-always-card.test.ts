// SHEP-18: the window chat's permission card (mods/chat/cards.js) offers Cursor's own Allow always where its bridge's
// request does (cursor_always), apart from Allow once and Deny, with what it adds; each control answers its own request
// as the bridge reads it. Claude's and Codex's cards keep Allow and Deny.
import { expect, mock, test } from "bun:test";
import { fakeGpuiBase, fakeGpuiKit } from "./fake-gpui";

mock.module("gpui-kit", () => fakeGpuiKit());
mock.module("gpui-base", () => fakeGpuiBase());
const { AskCards } = await import("../mods/chat/cards.js");
const { words } = await import("../mods/chat/core/asks.js");

const cursorReq = (id: string, extra: object = {}) => ({ tool_name: "Bash", tool_input: { command: `gh pr view ${id}`, description: "Not in allowlist: gh" },
  cwd: "/x", herdr_agent: "cursor", cursor_always: { option: "allow-always", name: "Allow always", scope: "adds Shell(gh) to Cursor's allowlist, for every such command" }, ...extra });
const askOf = (id: string, req: any) => ({ id, req, words: words(req) });

function cards() {
  const sent: { id: string; text: string }[] = [];
  const c = new AskCards({ reply: (ask: any, text: string) => sent.push({ id: ask.id, text }), toTerminal: () => {}, chatAbout: () => {} }, { focus: () => {} });
  return { c, sent };
}
const ctls = (strips: any[]) => strips.filter((s) => s.kind === "controls").flatMap((s) => s.ctls);
const decision = (text: string) => JSON.parse(text).hookSpecificOutput.decision;

test("a Cursor permission offering Allow always: Allow once and Deny, then what it adds and Allow always on its own", () => {
  const { c, sent } = cards();
  const a = askOf("t9.100.1", cursorReq("1"));
  const strips = c.strips(a, { who: "Cursor" });
  expect(ctls(strips).map((x: any) => [x.label, x.look])).toEqual([["Allow once", "primary"], ["Deny", "secondary"], ["Allow always", "secondary"]]);
  // the actions row holds only the two; Allow always is its own row, under its scope
  const keys = strips.map((s: any) => s.key);
  expect(keys.slice(keys.indexOf("actions"))).toEqual(["actions", "always-scope", "always"]);
  expect((strips as any[]).find((s) => s.key === "always-scope")?.text).toBe("Allow always: adds Shell(gh) to Cursor's allowlist, for every such command.");
  ctls(strips).find((x: any) => x.label === "Allow always").fn({});
  expect(sent).toEqual([{ id: "t9.100.1", text: expect.any(String) }]);
  expect(decision(sent[0].text)).toEqual({ behavior: "allow", cursorOption: "allow-always" });
});

test("its Allow once and Deny answer as before, each its own request (two at once)", () => {
  const { c, sent } = cards();
  const one = askOf("t9.100.1", cursorReq("1")), two = askOf("t9.101.1", cursorReq("2"));
  ctls(c.strips(two, {})).find((x: any) => x.label === "Allow once").fn({});
  ctls(c.strips(one, {})).find((x: any) => x.label === "Deny").fn({});
  expect(sent.map((s) => [s.id, decision(s.text)])).toEqual([["t9.101.1", { behavior: "allow" }], ["t9.100.1", { behavior: "deny", message: "The person declined this in the chat." }]]);
});

test("no Allow always where Cursor offers none or its scope can't be said, nor on Claude's or a plan's card", () => {
  const { c } = cards();
  const plain = [
    askOf("a", cursorReq("1", { cursor_always: undefined })),
    askOf("b", cursorReq("1", { cursor_always: { option: "allow-always", name: "Allow always", scope: "" } })),
    askOf("c", cursorReq("1", { herdr_agent: undefined })),
    askOf("d", { ...cursorReq("1"), herdr_agent: "claude" }),
  ];
  for (const a of plain) {
    const strips = c.strips(a, {});
    expect(ctls(strips).map((x: any) => x.label)).toEqual(["Allow", "Deny"]);
    expect(strips.some((s: any) => s.key.startsWith("always"))).toBe(false);
  }
  const plan = askOf("e", { ...cursorReq("1"), tool_name: "ExitPlanMode", tool_input: { plan: "1. Do it" } });
  expect(ctls(c.strips(plan, {})).map((x: any) => x.label)).toEqual(["Approve", "Keep planning"]);
});

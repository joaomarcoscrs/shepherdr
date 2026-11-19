import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { SHOWN, NEEDS_YOU, FINISHED, toastTitle, assistantWho, taskWho, together, needReason, withToast, unexpired, tone } from "./toasts.js";
import { REASONS } from "./needs.js";

const root = join(import.meta.dir, "..");
const read = (p) => readFileSync(join(root, p), "utf8");

test("titles say who and what, as the window's", () => {
  expect(toastTitle("HERD-4", NEEDS_YOU)).toBe("HERD-4 · Needs you");
  expect(toastTitle(assistantWho("tt-repo"), FINISHED)).toBe("tt-repo Assistant · Finished");
  expect(assistantWho(null)).toBe("Assistant");
  // this computer's task by its ref alone; another machine's with that machine's name
  expect(taskWho("SHEP-17", null)).toBe("SHEP-17");
  expect(toastTitle(taskWho("SHEP-17", "MacBook"), NEEDS_YOU)).toBe("SHEP-17 on MacBook · Needs you");
});

test("one event is one toast: the same key takes the old one's place", () => {
  let l = withToast([], { title: "a", body: "Question for you", key: "need:x" }, 1000, "t1");
  l = withToast(l, { title: "b" }, 1001, "t2");
  l = withToast(l, { title: "a", body: "Rerun CI?", key: "need:x" }, 1002, "t3");
  expect(l.map((n) => [n.id, n.body ?? null])).toEqual([["t2", null], ["t1", "Rerun CI?"]]);
});

test("several at once in one line", () => {
  expect(together([{ need: true, task: true }, { need: true, task: true }])).toBe("2 tasks need you");
  expect(together([{ need: true, task: true }, { need: true }])).toBe("2 need you");
  expect(together([{ need: true }, {}, {}])).toBe("3 updates");
});

test("why it needs you", () => {
  expect(needReason({ question: { kind: "Plan", title: "x" } })).toBe(REASONS.plan);
  expect(needReason({ question: { kind: "Question", title: "Rerun CI?" } })).toBe("Rerun CI?");
  expect(needReason({ secret: { name: "API_KEY" } })).toBe("Asks for API_KEY");
  expect(needReason({})).toBe(REASONS.question);
});

test("kept and gone", () => {
  let l = [];
  for (let i = 0; i < 7; i++) l = withToast(l, { title: String(i) }, 1000 + i, `t${i}`);
  expect(l.map((n) => n.title)).toEqual(["2", "3", "4", "5", "6"]);
  expect(unexpired([{ at: 0, level: "alert" }, { at: 0 }], 10000).length).toBe(1);
  expect(tone("alert")).toBe("red");
  expect(SHOWN).toBe(3);
});

test("the window's toasts say the same words as these (mods/chat, lib/ui.js), and mods/toasts reads its rules here", () => {
  expect(read("mods/chat/main.js")).toContain("`${toastWho(k)} · Needs you`");
  expect(read("mods/chat/main.js")).toContain("`${toastWho(k)} · Finished`");
  expect(read("lib/inbox.js")).toContain("· Needs you`");
  expect(read("lib/ui.js")).toContain("`${name} Assistant` : \"Assistant\"");
  expect(read("mods/toasts/main.js")).toContain('from "./core/toasts.js"');
  expect(read("web/plugins/alerts/toasts.tsx")).toContain("@core/toasts.js");
  expect(read("service/push.ts")).toContain("../core/toasts.js");
});

// ---- a permission's choices on its toast (SHEP-19) ---------------------------------------------------------------
import { askToast, askAnswer, askLines, ASK_COLS, ASK_DENIED } from "./toasts.js";
import { allow, deny, allowAlways } from "./asks.js";

const bash = (command, extra = {}) => ({ tool_name: "Bash", tool_input: { command }, ...extra });
const cursorAlways = { option: "allow-always-7", name: "Allow always", scope: "adds Shell(gh) to Cursor's allowlist, for every such command" };

test("a short command: its card's choices, the command shown whole in lines that fit", () => {
  const v = askToast(bash('gh pr view 3358 --repo roboflow/inference --comments 2>&1 | rg -A5 "dkosowski87" | head -20'));
  expect(v.kind).toBe("choices");
  expect(v.lead).toBe("Run");
  expect(v.lines.join("")).toBe('gh pr view 3358 --repo roboflow/inference --comments 2>&1 | rg -A5 "dkosowski87" | head -20');
  expect(v.lines.every((l) => l.length <= ASK_COLS)).toBe(true);
  expect(v.choices.map((c) => c.label)).toEqual(["Allow", "Deny"]);
  expect(v.always).toBeUndefined();
});

test("Cursor's Allow always only where its card offers it, with what it adds shown whole; then Allow is Allow once", () => {
  const req = bash("gh pr list", { herdr_agent: "cursor", cursor_always: cursorAlways });
  const v = askToast(req);
  expect(v.choices.map((c) => c.label)).toEqual(["Allow once", "Deny", "Allow always"]);
  expect(v.always.lines.join("")).toBe("Allow always: adds Shell(gh) to Cursor's allowlist, for every such command.");
  // Claude Code's request never carries it, even with the field
  expect(askToast(bash("gh pr list", { cursor_always: cursorAlways })).choices.map((c) => c.id)).toEqual(["allow", "deny"]);
  // too long to say whole on a toast: Review (its card has it), never Allow once alone in its place
  const long = { ...cursorAlways, scope: `adds ${Array.from({ length: 12 }, (_, i) => `Shell(tool${i})`).join(", ")} to Cursor's allowlist` };
  expect(askToast(bash("a", { herdr_agent: "cursor", cursor_always: long })).kind).toBe("review");
});

test("what doesn't fit whole or plainly is Review, never choices", () => {
  expect(askToast(bash("x".repeat(ASK_COLS * 5 + 1))).kind).toBe("review");
  expect(askToast(bash("echo ok‮")).kind).toBe("review");         // a right-to-left mark
  expect(askToast(bash("printf 'a\tb'")).kind).toBe("review");         // a tab
  expect(askToast(bash("echo é")).kind).toBe("review");                // not plain ASCII
  expect(askToast({ tool_name: "Edit", tool_input: { file_path: "/a", old_string: "x", new_string: "y" } }).kind).toBe("review");
  expect(askToast({ tool_name: "Write", tool_input: { file_path: "/a", content: "x" } }).kind).toBe("review");
  expect(askToast({ tool_name: "ExitPlanMode", tool_input: { plan: "do it" } })).toEqual({ kind: "review", reason: "Plan to approve" });
  expect(askToast({ tool_name: "AskUserQuestion", tool_input: { questions: [{ question: "Tea?", options: ["Yes", "No"] }] } }).kind).toBe("review");
  expect(askToast(null).kind).toBe("review");
  expect(askToast(bash("")).kind).toBe("review");
  // a request the card cuts (past its lines) is never allowed from a toast
  expect(askToast(bash(Array.from({ length: 2100 }, () => "x").join("\n"))).kind).toBe("review");
  expect(askToast({ tool_name: "Read", tool_input: { file_path: "/tmp/a.txt" } }).lead).toBe("Read");
});

test("each line fits, every character kept in order", () => {
  for (const s of ["a b c", "x".repeat(100), `${"word ".repeat(20)}end`, "one\n\ntwo"]) {
    const l = askLines(s, 10);
    expect(l.every((x) => x.length <= 10)).toBe(true);
    expect(l.join("\n").replace(/\n/g, "")).toBe(s.replace(/\n/g, ""));
  }
});

test("a toast's answer is its card's, made from the request read again", () => {
  const req = bash("gh pr list", { herdr_agent: "cursor", cursor_always: cursorAlways });
  expect(askAnswer(req, "allow")).toBe(allow());
  expect(askAnswer(req, "deny")).toBe(deny(ASK_DENIED));
  expect(askAnswer(req, "always")).toBe(allowAlways(req));
  expect(JSON.parse(askAnswer(req, "always")).hookSpecificOutput.decision).toEqual({ behavior: "allow", cursorOption: "allow-always-7" });
  // a request that doesn't offer it (anymore) gets none: never a broader answer
  expect(askAnswer(bash("gh pr list"), "always")).toBeNull();
  expect(askAnswer(bash("x".repeat(500)), "allow")).toBeNull();
  expect(askAnswer(req, "dismiss")).toBeNull();
});

test("a permission's toast takes the place of its agent's Needs you, whichever came first; two requests stay two", () => {
  let l = withToast([], { title: "a · Needs you", key: "need:t1" }, 1000, "n1");
  l = withToast(l, { title: "a · Needs you", key: "ask:t1.1.9", covers: ["need:t1"] }, 1001, "a1");
  l = withToast(l, { title: "a · Needs you", key: "ask:t1.2.9", covers: ["need:t1"] }, 1002, "a2");
  expect(l.map((n) => n.key)).toEqual(["ask:t1.1.9", "ask:t1.2.9"]);
  // another agent's Needs you stays
  l = withToast([{ key: "need:t2", at: 1000, id: "x" }], { key: "ask:t1.1.9", covers: ["need:t1"] }, 1001, "y");
  expect(l.map((n) => n.key)).toEqual(["need:t2", "ask:t1.1.9"]);
});

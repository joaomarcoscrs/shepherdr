import { expect, test } from "bun:test";
import { codexBox, deleteKeys, hasReport, historyLines, isCodex, onlyReports, settle, settleIfCodex, stripReports } from "./mouse-leak.js";

// SHEP-13: reports Codex typed into its box ("[<65;101;61M", a wheel move whose ESC reached it apart from the rest)
const R1 = "[<65;101;61M", R2 = "[<64;101;61M";
const screen = (box) => `  history\n\n› ${box}\n\n  GPT-6.2-Sol low · ~/proj\n  ← for agents · ? for shortcuts\n`;

/** A Codex terminal: its box, keys pressed; onQuiet lands while hc waits for quiet before the F12. */
function term(box, { onQuiet = "", noBox = false } = {}) {
  const t = { box, keys: [] };
  t.io = {
    hc: async (args) => {
      if (args[0] === "read") return { code: 0, stdout: noBox ? "a picker\n" + "row\n".repeat(9) : screen(t.box) };
      const keys = args.slice(2);
      if (keys[0] === "f12") { t.box += onQuiet; onQuiet = ""; }
      t.keys.push(keys);
      for (const k of keys) if (k === "backspace") t.box = t.box.slice(0, -1);
      return { code: 0, stdout: "{}" };
    },
  };
  return t;
}

test("reports and words", () => {
  expect(onlyReports(R1 + R2)).toBe(true);
  expect(onlyReports(` ${R1} ${R2} `)).toBe(true);
  expect(onlyReports("fix it" + R1)).toBe(false);
  expect(onlyReports("see [<b>] tag")).toBe(false);
  expect(onlyReports("[<65;101;61")).toBe(false);
  expect(hasReport("a[<0;1;1mb")).toBe(true);
  expect(stripReports(R2 + R1 + "/model")).toBe("/model");
  expect(deleteKeys(R1)).toEqual(["end", ...Array(R1.length).fill("backspace")]);
  expect(deleteKeys("hi" + R1)).toBeNull();
  expect(isCodex({ status: { app: "codex" } })).toBe(true);
  expect(isCodex({ foreground: { name: "codex" } })).toBe(true);
  expect(isCodex({ status: { app: "claude" } })).toBe(false);
});

test("codexBox: the composer, never a line far up", () => {
  expect(codexBox(screen(R1))).toBe(R1);
  expect(codexBox("› a long one\n  that wraps\n\n  model")).toBe("a long onethat wraps");
  expect(codexBox("› old message\n" + "line\n".repeat(9))).toBeNull();
});

test("settle: waits for quiet, then deletes reports alone, the late one too", async () => {
  const t = term(R1, { onQuiet: R2 });
  expect(await settle(t.io, "t1")).toBe(true);
  expect(t.box).toBe("");
  expect(t.keys[0]).toEqual(["f12"]);
  expect(t.keys[1]).toEqual(["end", ...Array((R1 + R2).length).fill("backspace")]);
});

test("settle: words are never touched", async () => {
  for (const box of ["fix it" + R1, R1 + "please review", "see [<b>] tag", "hello"]) {
    const t = term(box);
    const ok = await settle(t.io, "t1");
    expect(t.box).toBe(box);
    expect(t.keys).toEqual([["f12"]]);
    expect(ok).toBe(!hasReport(box));
  }
});

test("settle: no box on screen (a picker): no keys at all", async () => {
  const t = term(R1, { noBox: true });
  expect(await settle(t.io, "t1")).toBe(true);
  expect(t.keys).toEqual([]);
});

test("settleIfCodex: other agents untouched; a failing check never stops the send", async () => {
  const t = term(R1);
  expect(await settleIfCodex(t.io, "t1", async () => ({ status: { app: "claude" } }))).toBe(true);
  expect(t.keys).toEqual([]);
  expect(await settleIfCodex(t.io, "t1", async () => { throw new Error("gone"); })).toBe(true);
});

// SHEP-14: herdr-core reads a full-screen app's history by scrolling it with wheel reports: never Codex's
test("historyLines: none from Codex, as asked from anything else", () => {
  expect(historyLines({ status: { app: "codex" } }, 2000)).toBe(0);
  expect(historyLines({ labels: { "herdr.agent": "codex" } }, 40)).toBe(0);
  expect(historyLines("codex", 2000)).toBe(0);
  expect(historyLines({ status: { app: "claude" } }, 40)).toBe(40);
  expect(historyLines("claude", 2000)).toBe(2000);
  expect(historyLines(null, 40)).toBe(40);
});

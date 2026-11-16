// SHEP-7: delivery profiles' shared words and rules: the Accept card's Hand-off (core/accept.js), the row's second line
// (core/tasks.js), the row menu (core/menus.js) and the settings' Open PR rows (core/settings.js).
import { expect, test } from "bun:test";
import { ACCEPT, acceptLabel, cardTitle, handoffWords } from "./accept.js";
import { openPr, secondLine } from "./tasks.js";
import { taskMenu } from "./menus.js";
import { DEFAULTS, DELIVERY, PR_REVIEW, TASK_ROWS, prAssign } from "./settings.js";

const H = "0123456789abcdef0123456789abcdef01234567";

test("an Open PR card takes the hand-off, never says Accept and merge", () => {
  const e = { merges: false, handoff: { pr: 7, review: "jarbas-local", verdict: "APPROVED", head: H, current: true, assignees: ["joao"], next: "Find a human reviewer" } };
  expect(acceptLabel(e)).toBe("Take it, no merge");
  expect(acceptLabel(e)).not.toBe(ACCEPT.acceptMerge);
  expect(cardTitle("SHEP-7", e)).toBe("Take SHEP-7?");
  // the merge profile's card is as before
  expect([acceptLabel({ merges: true }), cardTitle("SHEP-7", { merges: true })]).toEqual(["Accept and merge", "Accept SHEP-7?"]);
  expect(handoffWords(e.handoff)).toEqual([
    { text: "Handed off to you: Herdr never merges it", tone: "muted" },
    { text: "Jarbas Local approved 0123456, the pull request's head", tone: "ready" },
    { text: "Assigned to @joao", tone: "muted" },
    { text: "Next: Find a human reviewer", tone: "muted" },
  ]);
  expect(handoffWords(null)).toBeNull();
});

test("the approval is only ever for the exact head: moved, another verdict, none, or blocked say so", () => {
  const h = { pr: 7, review: "jarbas-local", head: H };
  expect(handoffWords({ ...h, verdict: "APPROVED", current: false })[1]).toEqual({ text: "Jarbas Local approved 0123456, but the pull request has moved since", tone: "need" });
  expect(handoffWords({ ...h, verdict: "REQUEST_CHANGES" })[1]).toEqual({ text: "Jarbas Local said request changes at 0123456", tone: "need" });
  expect(handoffWords({ ...h, verdict: null })[1].text).toBe("Jarbas Local hasn't approved 0123456");
  expect(handoffWords({ ...h, blocked: "Jarbas stopped  after four rounds" })[0]).toEqual({ text: "Handed off blocked: Jarbas stopped after four rounds", tone: "need" });
  expect(handoffWords({ pr: 7, review: "none", head: H, assignees: [] }).map((x) => x.text)).toEqual([
    "Handed off to you: Herdr never merges it", "No approval asked before the hand-off", "Not assigned to anyone", "Next: it's yours from here"]);
});

test("a handed-off row says so, and its menu never offers Accept when ready", () => {
  const task = { state: "ready", ready_at: 1, brief_delivery: { mode: "open_pr" }, handoff: { pr: 7 } };
  expect(openPr(task)).toBe(true);
  expect(openPr({ ...task, set: { delivery: "merge" } })).toBe(false);
  expect(openPr({ state: "running" })).toBe(false);  // briefed before profiles: merges
  expect(secondLine({ section: "ready", task })).toBe("Handed off · PR #7");
  expect(secondLine({ section: "ready", task: { ...task, handoff: { blocked: "x" } } })).toBe("Handed off blocked");
  const ids = (o) => taskMenu(o).map((x) => x.id).filter(Boolean);
  expect(ids({ status: "working" })).toContain("auto-accept");
  expect(ids({ status: "working", openPr: true })).not.toContain("auto-accept");
  // on already (from before): offered only to turn it off
  expect(ids({ status: "ready", openPr: true, autoAccept: true })).toContain("auto-accept");
});

test("the settings rows: Merge by default, Open PR's approval, and GitHub logins", () => {
  expect(DEFAULTS.tasks.delivery).toBe("merge");
  expect(DELIVERY).toEqual([["merge", "Merge"], ["open_pr", "Open PR"]]);
  expect(PR_REVIEW).toEqual([["none", "None"], ["jarbas-local", "Jarbas Local"]]);
  expect(TASK_ROWS.slice(0, 3).map((r) => r[1])).toEqual(["delivery", "pr_review", "merge"]);
  expect(prAssign(" joao, @ana joao ")).toBe("joao,ana");
  expect(prAssign("")).toBe("");
  expect(prAssign("not a login!")).toBeNull();
  expect(prAssign("-bad")).toBeNull();
});

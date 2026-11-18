// SHEP-15: Awaiting review as the Tasks panel and the web app place it (core/tasks.js): its own section and group,
// its review signal as its second line and its dot's tone, never Done nor Needs you, and finished for --after.
import { expect, test } from "bun:test";
import { AWAITING, ORDER, RANK_OF, REVIEW, STATUS, finished, order, place, queueReason, reviewLine, reviewOf, reviewTone, secondLine } from "./tasks.js";
import { whereNow } from "./accept.js";

const list = { prefix: "ROBO", folder: "/r", tasks: [] };
const awaiting = (r = {}, extra = {}) => ({ id: 9, title: "WF Evals at 2,000 images", state: "awaiting",
  awaiting: { since: 5, pr: 19756, repo: "roboflow/roboflow", url: "https://github.com/roboflow/roboflow/pull/19756", head: "c130a7f" },
  ...(r ? { pr_review: { pr: 19756, ...r } } : {}), ...extra });

test("its own section, after Working, labelled Awaiting review", () => {
  expect(STATUS[AWAITING]).toBe("Awaiting review");
  expect(ORDER.indexOf("awaiting")).toBe(ORDER.indexOf("working") + 1);
  expect(RANK_OF.awaiting).toBeLessThan(RANK_OF.done);
  const p = place(list, awaiting({ state: "approved", text: "Approved" }), { live: [{ address: "t1", status: "idle" }] });
  // never Needs you (no live agent asked), never Done: its signal why
  expect([p.section, p.needs, p.why, p.live]).toEqual(["awaiting", null, "Approved", []]);
});

test("its second line says the pull request and the review, and what's unknown", () => {
  const line = (r) => secondLine(place(list, awaiting(r)), null);
  expect(line({ state: "awaiting_approval", text: "Awaiting approval" })).toBe("PR #19756 · Awaiting approval");
  expect(line({ state: "changes_requested", text: "Changes requested", head_moved: true })).toBe("PR #19756 · Changes requested · head moved");
  expect(line({ state: "unavailable", text: "GitHub unavailable", was: "approved" })).toBe("PR #19756 · GitHub unavailable (was approved)");
  // never read yet (an older client's record, or just accepted)
  expect(line(null)).toBe("PR #19756 · Checking GitHub");
  expect(reviewOf(awaiting({ state: "weird" })).state).toBe("unknown");
  expect(reviewLine(awaiting({ state: "closed" }))).toBe("PR #19756 · Closed without merging");
  expect(Object.keys(REVIEW)).toEqual(["awaiting_approval", "approved", "changes_requested", "draft", "closed", "merged", "unknown", "unavailable"]);
});

test("its dot's tone: green approved, red changes or closed, amber waiting, grey unknown", () => {
  expect(["approved", "changes_requested", "closed", "awaiting_approval", "draft", "unknown", "unavailable"].map(reviewTone))
    .toEqual(["ready", "need", "need", "work", "work", "muted", "muted"]);
});

test("--after counts it finished (it was Done before), and newest hand-off first", () => {
  expect([finished("awaiting"), finished("done"), finished("ready")]).toEqual([true, true, false]);
  const l = { ...list, tasks: [awaiting(), { id: 10, state: "queued", after: [9] }] };
  expect(queueReason(l, l.tasks[1], 0)).toBe("Starts soon");
  const a = place(list, awaiting({}, { awaiting: { since: 1 } })), b = place(list, { ...awaiting(), id: 3, awaiting: { since: 2 } });
  expect([a, b].sort(order("awaiting")).map((p) => p.task.id)).toEqual([3, 9]);
});

test("the Accept card says it was taken already", () => {
  expect(whereNow({ state: "awaiting" })).toBe("Already taken: awaiting review on GitHub.");
});

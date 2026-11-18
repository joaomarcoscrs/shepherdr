// SHEP-16: Awaiting deploy as the Tasks panel and the web app place it (core/tasks.js): its own section and group after
// Awaiting review, its deploys as its second line and its dot's tone, never Done nor Needs you, and finished for --after.
import { expect, test } from "bun:test";
import { DEPLOY, DEPLOYING, ORDER, RANK_OF, STATUS, deployLine, deployOf, deployTone, finished, place, queueReason, secondLine } from "./tasks.js";
import { whereNow } from "./accept.js";

const list = { prefix: "ROBO", folder: "/r", tasks: [] };
const target = (key, status, workflow = "function") => ({ key, label: key, workflow, status, environment: "prod", ref: "master" });
const deploying = (d = {}, extra = {}) => ({ id: 9, title: "WF Evals at 2,000 images", state: "deploying", merged: true,
  deploy: { since: 5, pr: 19756, url: "https://github.com/roboflow/roboflow/pull/19756", landing: { state: "landed", sha: "m" }, ...d }, ...extra });

test("its own section, after Awaiting review, labelled Awaiting deploy", () => {
  expect(STATUS[DEPLOYING]).toBe("Awaiting deploy");
  expect(ORDER.indexOf("deploying")).toBe(ORDER.indexOf("awaiting") + 1);
  expect(RANK_OF.deploying).toBeLessThan(RANK_OF.done);
  const p = place(list, deploying({ state: "running", text: "Deploying" }), { live: [{ address: "t1", status: "blocked" }] });
  // never Needs you (an agent left asking doesn't count), never Done: its deploy state why
  expect([p.section, p.needs, p.why, p.live]).toEqual(["deploying", null, "Deploying", []]);
});

test("its second line says the pull request, its deploys and what's unknown", () => {
  const line = (d) => secondLine(place(list, deploying(d)), null);
  const targets = [target("function:a", "success"), target("function:b", "running"), target("hosting", "ready", "hosting"),
    target("manual:deploy-rules.yaml", "manual", "manual")];
  expect(line({ state: "running", text: "Deploying", targets })).toBe("PR #19756 · Deploying · 1 of 3 passed");
  // stacked, not on master yet: which pull request lands it
  expect(line({ state: "waiting", text: "Waiting for master", landing: { state: "waiting", via: { pr: 19700 } }, targets }))
    .toBe("PR #19756 · Waiting for master · after #19700");
  expect(line({ state: "ready", text: "GitHub unavailable", was: "failed", targets })).toBe("PR #19756 · GitHub unavailable (was deploy failed) · 1 of 3 passed");
  expect(line({ landing: null })).toBe("PR #19756 · Checking GitHub");
  expect(deployOf(deploying({ state: "weird" })).state).toBe("unknown");
  expect(deployLine(deploying({ state: "unavailable" }))).toBe("PR #19756 · Deploy plan unavailable");
  expect(Object.keys(DEPLOY)).toEqual(["waiting", "unavailable", "manual", "ready", "running", "failed", "success", "none", "unknown"]);
});

test("its dot's tone: green deployed or ready, red failed or unclear, amber running or waiting", () => {
  expect(["success", "none", "ready"].map(deployTone)).toEqual(["ready", "ready", "ready"]);
  expect(["failed", "manual", "unavailable"].map(deployTone)).toEqual(["need", "need", "need"]);
  expect(["running", "waiting", "unknown"].map(deployTone)).toEqual(["work", "work", "muted"]);
});

test("finished for --after, and the Accept card says where it is", () => {
  expect(finished("deploying")).toBe(true);
  const l = { ...list, tasks: [deploying(), { id: 10, title: "next", state: "queued", after: [9] }] };
  expect(queueReason(l, l.tasks[1], 0)).not.toContain("after");
  expect(whereNow({ state: "deploying" })).toBe("Already taken and merged: awaiting deploy.");
});

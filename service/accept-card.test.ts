// SHEP-4: the window's Accept card for a Roboflow task: Try it links its pull request and its ephemeral environment,
// the environment only once herdr-task's evidence says Ready; any other task's card has no Try it.
import { beforeEach, expect, test } from "bun:test";
import { mock } from "bun:test";
import { fakeHerdr } from "./fake-herdr";
import { built, byId, dispatch, fakeGpuiBase, fakeGpuiKit } from "./fake-gpui";

const colors = new Proxy({}, { get: () => "#808080" });
const none = async () => ({ code: 1, stdout: "", stderr: "" });
mock.module("herdr", () => fakeHerdr({ exec: none, props: () => ({ ref: "RF-1", folder: "/tmp/rf" }), theme: () => colors, shared: () => ({}), appHome: () => "/tmp/app" }));
mock.module("gpui-kit", () => fakeGpuiKit());
mock.module("gpui-base", () => fakeGpuiBase());
(globalThis as any).localStorage ??= { getItem: () => null, setItem: () => {}, removeItem: () => {} };

const { default: AcceptCard } = await import("../mods/accept-card/main.js");

const PR = { number: 19640, url: "https://github.com/roboflow/roboflow/pull/19640" };
const CHECK = "https://github.com/roboflow/roboflow/runs/133487312816";
const card = (extra: object) => {
  const c: any = Object.create(AcceptCard.prototype);
  Object.assign(c, { handle: {}, details: false, busy: false, error: null,
    t: { state: "ready", title: "x", evidence: { summary: "s", git: true, commits: { count: 0, list: [] }, diff: null, ...extra } } });
  built.length = 0;
  c.render({});
  return c;
};
// the url an element's click opens in the browser
const opened = (id: string) => {
  const urls: string[] = [];
  dispatch(byId(id), "on_click", { open_url: (u: string) => urls.push(u) });
  return urls;
};
const words = () => JSON.stringify(built.flatMap((e: any) => e.kids.filter((k: unknown) => typeof k === "string")));

beforeEach(() => { built.length = 0; });

test("ready: the pull request and the environment, each opening its own link", () => {
  card({ pr: PR, ephemeral: { state: "ready", url: "https://pr-19640.eph.roboflow.one", host: "pr-19640.eph.roboflow.one", check_url: CHECK } });
  expect(words()).toContain("Try it");
  expect(opened("open-pr")).toEqual([PR.url]);
  expect(opened("open-eph")).toEqual(["https://pr-19640.eph.roboflow.one"]);
  expect(byId("eph-check")).toBeUndefined();
});

test("not ready: no environment link, only where it stands and its check on GitHub", () => {
  for (const ephemeral of [{ state: "starting", phase: "Building", check_url: CHECK }, { state: "failed", phase: "Environment failed", check_url: CHECK },
    { state: "gone", why: "closed", check_url: CHECK }]) {
    card({ pr: PR, ephemeral });
    expect(byId("open-eph")).toBeUndefined();
    expect(opened("open-pr")).toEqual([PR.url]);
    expect(opened("eph-check")).toEqual([CHECK]);
  }
  card({ pr: PR, ephemeral: { state: "none" } });
  expect(byId("open-eph")).toBeUndefined();
  expect(byId("eph-check")).toBeUndefined();
  expect(words()).toContain("None for this pull request");
});

test("any other task: no Try it, as before", () => {
  card({ checks: { state: "passed", text: "All 5 passed" } });
  expect(words()).not.toContain("Try it");
  expect(byId("open-pr")).toBeUndefined();
  expect(byId("open-eph")).toBeUndefined();
});

// SHEP-7: an Open PR task's card in the window: its Hand-off (the pull request, its approval for the exact head, who it's
// assigned to, what's next) and a button that takes it, never "Accept and merge"; the ephemeral environment still shows
const HEAD = "0123456789abcdef0123456789abcdef01234567";
const handoff = { pr: 19640, url: PR.url, review: "jarbas-local", verdict: "APPROVED", head: HEAD, current: true,
  comment: `${PR.url}#issuecomment-1`, assignees: ["joao"], next: "Find a human reviewer", state: "open" };

test("an Open PR hand-off: its section, its links, and Take it, no merge", () => {
  card({ merges: false, delivery: "open_pr", handoff, pr: PR, ephemeral: { state: "ready", url: "https://pr-19640.eph.roboflow.one", host: "pr-19640.eph.roboflow.one" } });
  const w = words();
  for (const x of ["Hand-off", "Take RF-1?", "Jarbas Local approved 0123456, the pull request's head", "Assigned to @joao", "Next: Find a human reviewer", "Try it"]) expect(w).toContain(x);
  expect(w).toContain("Take it, no merge");
  expect(w).not.toContain("Accept and merge");
  expect(opened("open-handoff-pr")).toEqual([PR.url]);
  expect(opened("handoff-review")).toEqual([handoff.comment]);
  expect(opened("open-eph")).toEqual(["https://pr-19640.eph.roboflow.one"]);
  expect(byId("open-pr")).toBeUndefined();  // its pull request once, in the Hand-off
});

test("a hand-off whose pull request moved since its approval says so", () => {
  card({ merges: false, handoff: { ...handoff, current: false } });
  expect(words()).toContain("Jarbas Local approved 0123456, but the pull request has moved since");
});

import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ACCEPT, acceptLabel, words, readable, whereNow, commitsLine, filesLine, moreLine, proofTitle, ephemeralWords } from "./accept.js";

const root = join(import.meta.dir, "..");
const read = (p) => readFileSync(join(root, p), "utf8");

test("a long old summary keeps its first sentence; the whole goes under Details", () => {
  const long = `Fixed the login. ${"More words here. ".repeat(12)}`;
  const w = words({ summary: long });
  expect(w.summary).toBe("Fixed the login. More words here. More words here. More words here. More words here. More words here. More words here. More words here. More words here.");
  expect(w.details).toBe(long.trim());
  expect(words({ summary: "Short.", details: "x" })).toEqual({ summary: "Short.", details: "x" });
});

test("plain details become bullets, clear paths as code; markdown stays", () => {
  expect(readable("Changed web/src/App.tsx. The tests pass now; all good")).toBe("- Changed `web/src/App.tsx`\n- The tests pass now; all good");
  expect(readable("- one\n- two")).toBe("- one\n- two");
});

test("the card's words", () => {
  expect(ACCEPT.title("HERD-4")).toBe("Accept HERD-4?");
  expect(whereNow({ state: "done", accepted_at: 1 })).toBe("Already accepted.");
  expect(whereNow({ state: "done" })).toBe("Already done.");
  expect(whereNow({ state: "work", status_text: "Working" })).toBe("Not ready right now: it's in Working.");
  expect([commitsLine(1), filesLine(3), moreLine(2)]).toEqual(["1 commit", "3 files changed", "And 2 more"]);
  expect(proofTitle("HERD-4: Login works")).toBe("Login works");
});

// SHEP-225 (#1046): the button says what pressing it does
test("Accept says whether it merges", () => {
  expect(acceptLabel({ merges: true })).toBe("Accept and merge");
  expect(acceptLabel({ merges: false })).toBe("Accept");
  expect(acceptLabel(undefined)).toBe("Accept"); // still being read
  const mod = read("mods/accept-card/main.js"), web = read("web/src/screens/Accept.tsx");
  expect(mod).toContain("acceptLabel(e)");
  expect(web).toContain("A.acceptLabel(e)");
});

test("the window's card and the web's read their words from here, not their own", () => {
  const mod = read("mods/accept-card/main.js"), web = read("web/src/screens/Accept.tsx");
  expect(mod).toContain('from "./core/accept.js"');
  expect(web).toContain("@core/accept.js");
  for (const w of ["Reading the task", "No summary.", "No proof artifact.", "Already accepted.", "Not ready right now"]) {
    expect(mod).not.toContain(w);
    expect(web).not.toContain(w);
  }
});

test("SHEP-4: a Roboflow task's environment in words; only Ready reads as ready", () => {
  const now = Date.UTC(2026, 10, 16, 18);
  expect(ephemeralWords({ state: "ready", url: "https://pr-1.eph.roboflow.one", expires: now + 71 * 3600000 }, now)).toEqual({ text: "Ready for 2 more days", tone: "ready" });
  expect(ephemeralWords({ state: "ready", expires: now + 5 * 3600000 }, now).text).toBe("Ready for 5 more hours");
  expect(ephemeralWords({ state: "ready", expires: now + 20 * 60000 }, now).text).toBe("Ready for 20 more minutes");
  expect(ephemeralWords({ state: "ready" }, now).text).toBe("Ready");
  expect(ephemeralWords({ state: "starting", phase: "Building" })).toEqual({ text: "Not ready yet (building)", tone: "work" });
  expect(ephemeralWords({ state: "starting", phase: null }).text).toBe("Asked for, not started yet");
  expect(ephemeralWords({ state: "starting", phase: "HealthChecking" }).text).toBe("Not ready yet (health checking)");
  // a real failed check's title (roboflow/roboflow#19466)
  expect(ephemeralWords({ state: "failed", phase: "Environment failed" })).toEqual({ text: "Failed to start", tone: "need" });
  expect(ephemeralWords({ state: "gone", why: "closed" }).text).toBe("Torn down, as its pull request is closed");
  expect(ephemeralWords({ state: "gone", why: "unlabelled" }).text).toBe("Being torn down, as its label was taken off");
  expect(ephemeralWords({ state: "gone", why: "expired" }).text).toBe("Torn down, as its time ran out");
  expect(ephemeralWords({ state: "gone", phase: "Environment destroyed" })).toEqual({ text: "Torn down", tone: "muted" });
  expect(ephemeralWords({ state: "none" })).toEqual({ text: "None for this pull request", tone: "muted" });
  expect(ephemeralWords({ state: "unknown" })).toEqual({ text: "Couldn't ask GitHub about it", tone: "need" });
  expect(ephemeralWords({ state: "unknown", why: "link" }).tone).toBe("need");
  for (const s of ["starting", "failed", "gone", "none", "unknown"]) expect(ephemeralWords({ state: s }).tone).not.toBe("ready");
});

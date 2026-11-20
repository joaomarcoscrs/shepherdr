import { expect, test } from "bun:test";
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ChatReader } from "./chat-reader.js";
import { argvFor, current, menuFor, modelSections, parseDefaults, parsePicks, rowFor } from "./agents.js";

const id = (n) => `10000000-0000-4000-8000-00000000000${n}`;
const model = "custom-dialog-model";
const guardian = "codex-auto-review";
const state = (model) => ({ type: "turn_context", payload: { model } });
const message = (role, text) => ({ type: "response_item", payload: { type: "message", role,
  content: [{ type: role === "user" ? "input_text" : "output_text", text }] } });
const words = (r) => r.conv?.items.filter((x) => x.kind === "user" || x.kind === "agent").map((x) => x.text);
const write = (path, ...rows) => writeFileSync(path, rows.map((row) => JSON.stringify(row) + "\n").join(""));
const header = (n, meta) => ({ type: "session_meta", payload: { id: id(n), ...meta } });
const io = { sh: async (script, args) => {
  const p = Bun.spawn(["sh", "-c", script, ...args], { stdout: "pipe", stderr: "pipe" });
  return { stdout: await new Response(p.stdout).text(), code: await p.exited };
} };

async function fixture(run) {
  const dir = mkdtempSync(join(realpathSync(tmpdir()), "codex-parent-"));
  const paths = Array.from({ length: 6 }, (_, n) => join(dir, `rollout-2026-01-01T00-00-00-${id(n)}.jsonl`));
  write(paths[0], header(0, { source: { subagent: { other: "guardian" } } }), state(guardian),
    message("user", "Synthetic approval request"), message("assistant", '{"decision":"allow"}'));
  write(paths[1], header(1, { source: { subagent: "review" } }), message("assistant", "Sibling reply"));
  write(paths[2], header(2, { source: "cli", forked_from_thread_id: id(5) }), state(model),
    message("user", "Please count the notes."), message("assistant", "There are three notes."));
  write(paths[3], header(3, { source: "cli" }), message("assistant", "Explicit helper terminal"));
  write(paths[4], header(5, { source: "cli" }), message("assistant", "Mismatched metadata"));
  // A file can be open before its first record arrives.
  writeFileSync(paths[5], "");
  const program = Bun.spawn([process.execPath, "-e", `
    const { openSync, closeSync } = require("node:fs");
    const { createInterface } = require("node:readline");
    let fds = [];
    createInterface({ input: process.stdin }).on("line", (line) => {
      for (const fd of fds) closeSync(fd);
      fds = JSON.parse(line).map((path) => openSync(path, "r"));
      console.log("ready");
    });
  `], { stdin: "pipe", stdout: "pipe" });
  const out = program.stdout.getReader();
  const open = async (...ns) => {
    const response = out.read();
    program.stdin.write(JSON.stringify(ns.map((n) => paths[n])) + "\n");
    program.stdin.flush();
    expect(new TextDecoder().decode((await response).value).trim()).toBe("ready");
  };
  const term = {
    state: "live", pid: program.pid,
    foreground: { name: "codex", pid: program.pid, argv: ["codex", "fork", id(5)] },
    labels: { "herdr.resume": JSON.stringify({ kind: "codex", session: id(0), log: paths[0], pid: program.pid }) },
  };
  try {
    await run({ paths, open, term });
  } finally {
    out.releaseLock();
    program.kill();
    await program.exited;
    rmSync(dir, { recursive: true, force: true });
  }
}

test("guardian first, wrong saved label and stale reader still resolve to the foreground's fork", async () => {
  await fixture(async ({ paths, open, term }) => {
    await open(0, 1, 2);
    const reader = new ChatReader(io, "t1");
    // An old follower had accepted the first FD and already loaded its messages and model.
    Object.assign(reader, { path: paths[0], kind: "codex", pid: term.pid });
    await reader.pull();
    expect(reader.conv.meta.model).toBe(guardian);
    expect(await reader.locate(term)).toBe(true);
    expect(await reader.pull()).toBe(true);
    expect(reader.path).toBe(paths[2]);
    expect(words(reader)).toEqual(["Please count the notes.", "There are three notes."]);
    expect(rowFor("codex", current("codex", [], reader.conv.meta, {}))).toBe(model);
    await open(2, 0, 1);
    reader.locatedAt = 0;
    expect(await reader.locate(term)).toBe(false);
    expect(reader.path).toBe(paths[2]);
  });
});

test("internal, malformed and ambiguous rollouts wait for a unique parent, then follow a new session on the same PID", async () => {
  await fixture(async ({ paths, open, term }) => {
    await open(0, 1, 4, 5);
    const reader = new ChatReader(io, "t1");
    await reader.locate(term);
    expect(reader.path).toBeNull();
    expect(await reader.pull()).toBe(false);
    write(paths[5], header(5, { source: "cli" }), message("assistant", "Resumed conversation"));
    await reader.locate(term);
    expect(reader.path).toBe(paths[5]);
    await open(0, 2, 5);
    reader.locatedAt = 0;
    await reader.locate(term);
    expect(reader.path).toBeNull();
    await open(1, 3, 0);
    await reader.locate({ ...term, labels: { "herdr.role": "helper" } });
    await reader.pull();
    expect(words(reader)).toEqual(["Explicit helper terminal"]);
  });
});

test("saved task chats reject internal rollouts and read valid sessions", async () => {
  await fixture(async ({ paths }) => {
    const internal = new ChatReader(io, "log").fixed(paths[0], "codex", id(0));
    await internal.pull();
    expect(internal.state).toBe("missing");
    expect(internal.conv).toBeNull();
    const mismatched = new ChatReader(io, "log").fixed(paths[2], "codex", id(3));
    expect(await mismatched.pull()).toBe(false);
    const valid = new ChatReader(io, "log").fixed(paths[2], "codex", id(2));
    expect(await valid.pull()).toBe(true);
    expect(words(valid)).toEqual(["Please count the notes.", "There are three notes."]);
  });
});

test("the selected Codex model and old picks exclude the approval reviewer, while custom IDs still work", () => {
  const defaults = parseDefaults("codex", `model\tdemo-codex\nmodels\t${JSON.stringify({
    models: [{ slug: "demo-codex", display_name: "Demo Codex" }, { slug: guardian, visibility: "hide" }],
  })}`);
  const c = current("codex", ["codex", "--approve-for-me"], { model: guardian, effort: "low", speed: "fast" }, defaults);
  expect(c).toMatchObject({ model: "demo-codex", perm: "auto", fast: false });
  expect(c.effort).toBeNull();
  const row = rowFor("codex", c);
  const menus = { codex: menuFor("codex", defaults, row) };
  expect(menus.codex.models.filter((m) => m.id === row).map((m) => m.id)).toEqual(["demo-codex"]);
  const picks = { pinned: [`codex:${guardian}`], recent: [`codex:${model}`] };
  expect(parsePicks(JSON.stringify(picks))).toEqual({ pinned: [], recent: picks.recent });
  expect(modelSections(menus, "codex", picks, "").flatMap((s) => s.rows.map((m) => m.id))).toEqual([model, "demo-codex"]);
  expect(modelSections(menus, "codex", picks, guardian)).toEqual([]);
  expect(menuFor("codex", defaults, guardian).models.map((m) => m.id)).toEqual(["demo-codex"]);
  expect(menuFor("codex", defaults, model).models[0].id).toBe(model);
  expect(argvFor({ agent: "codex", model, perm: "auto" }, { agent: "codex" }, { codexAuto: true }).argv)
    .toContain("--approve-for-me");
  expect(() => argvFor({ agent: "codex", model: guardian }, { agent: "codex" })).toThrow();
});

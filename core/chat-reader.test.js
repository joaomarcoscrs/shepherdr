import { expect, test } from "bun:test";
import { appendFileSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { ChatReader, agentKindOf, kindOf, nativeCursorTerminal } from "./chat-reader.js";

test("Cursor's chat bridge is distinct from a native worker; both are Cursor agents", () => {
  expect(kindOf({ status: { app: "cursor", state: "idle" }, foreground: { name: "python3" } })).toBe("cursor");
  expect(kindOf({ status: null, foreground: { name: "Python", argv: ["/usr/bin/python3", "/h/.local/bin/herdr-task", "cursor-chat", "--model", "x"] } })).toBe("cursor");
  // herdr-task's other commands are no agent
  expect(kindOf({ foreground: { name: "python3", argv: ["python3", "/h/.local/bin/herdr-task", "list"] } })).toBe(null);
  const tui = { labels: { "herdr.agent": "cursor" }, foreground: { name: "node", argv: ["node", "/x/cursor-agent/index.js"] } };
  expect(kindOf(tui)).toBe("cursor");
  expect(nativeCursorTerminal(tui)).toBe(true);
  expect(nativeCursorTerminal({ ...tui, foreground: { argv: ["python3", "/h/bin/herdr-task", "cursor-chat"] } })).toBe(false);
  expect(nativeCursorTerminal({ ...tui, status: { app: "claude-code" } })).toBe(true); // the last program's report
  expect(nativeCursorTerminal({ ...tui, foreground: { name: "codex" }, status: null })).toBe(false);
  expect(agentKindOf(tui)).toBe("cursor");
  expect(kindOf({ status: { app: "claude-code" } })).toBe("claude");
  expect(kindOf({ status: { app: "codex" } })).toBe("codex");
});

const ID = "11111111-2222-3333-4444-555555555555";
const SIBLING = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const line = (role, content) => JSON.stringify({ role, message: { content } }) + "\n";
const words = (r) => r.conv?.items.filter((x) => x.kind === "user" || x.kind === "agent").map((x) => x.text);

async function fixture(run) {
  const home = mkdtempSync(join(realpathSync(tmpdir()), "shep-cursor-"));
  const root = join(home, ".cursor");
  const log = (id) => join(root, "projects", "same worktree", "agent-transcripts", id, id + ".jsonl");
  const store = (id) => join(root, "chats", "ab12", id, "store.db");
  const processes = [];
  const open = async (...ids) => {
    const p = Bun.spawn([process.execPath, "-e", 'const fs = require("node:fs"); for (const file of Bun.argv.slice(1)) fs.openSync(file, "r"); console.log("ready"); setInterval(() => {}, 1000)', ...ids.map(store)], { stdout: "pipe" });
    processes.push(p);
    const output = p.stdout.getReader();
    const ready = await output.read();
    output.releaseLock();
    expect(new TextDecoder().decode(ready.value).trim()).toBe("ready");
    return p.pid;
  };
  const terminal = (pid, record = {}, label) => ({
    state: "live", pid: 99, status: null, foreground: { name: "node", pid, argv: ["node", "/x/cursor-agent/index.js"] },
    labels: { "herdr.task": "demo", "herdr.resume": label ?? JSON.stringify({ kind: "cursor", session: ID, log: log(ID), pid: 99, ...record }) },
  });
  const calls = [];
  const io = { sh: async (script, args, machine) => {
    calls.push([args, machine]);
    const p = Bun.spawn(["sh", "-c", script, ...args], { env: { ...process.env, HOME: home, CURSOR_CONFIG_DIR: root }, stdout: "pipe" });
    return { stdout: await new Response(p.stdout).text(), code: await p.exited };
  } };
  try {
    for (const id of [ID, SIBLING]) {
      for (const path of [store(id), log(id)]) {
        mkdirSync(dirname(path), { recursive: true });
        writeFileSync(path, "");
      }
    }
    await run({ io, calls, log, open, terminal, home, root });
  } finally {
    for (const p of processes) p.kill();
    await Promise.all(processes.map((p) => p.exited));
    rmSync(home, { recursive: true, force: true });
  }
}

test("a native worker follows only its verified transcript through dirty writes, switch and resume", async () => {
  await fixture(async ({ io, calls, log, open, terminal }) => {
    const visible = line("user", [{ type: "text", text: "<user_query>\nCafé with Cursor\n</user_query>" }, { type: "image", source: { data: "a".repeat(400000) } }])
      + "{bad json\n" + JSON.stringify({ type: "status", status: "permission" }) + "\n"
      + line("assistant", [{ type: "thinking", text: "hidden reasoning" }, { type: "text", text: "Visible answer" }, { type: "tool_use", name: "Read", input: { file_path: "notes.md" } }])
      + line("user", [{ type: "tool_result", content: "hidden tool output" }]);
    const next = line("assistant", "Appended answer");
    writeFileSync(log(ID), visible + next.slice(0, -3));
    writeFileSync(log(SIBLING), line("assistant", "Sibling only"));
    const pid = await open(ID), siblingPid = await open(SIBLING);
    const reader = new ChatReader(io, "somewhere/t1");
    expect(await reader.locate(terminal(pid))).toBe(true);
    expect(await reader.pull()).toBe(true);
    expect(reader.format).toBe("cursor-native");
    expect(words(reader)).toEqual(["Café with Cursor", "Visible answer"]);
    expect(reader.conv.items[0].images).toBe(1);
    expect(reader.conv.items.at(-1).steps[0].line).toBe("Read notes.md");
    expect(reader.conv.questions.size).toBe(0);
    expect(await reader.pull()).toBe(false);
    appendFileSync(log(ID), next.slice(-3));
    expect(await reader.pull()).toBe(true);
    expect(words(reader)).toEqual(["Café with Cursor", "Visible answer", "Appended answer"]);

    expect(await reader.locate(terminal(siblingPid))).toBe(true);
    expect(reader.path).toBeNull();
    expect(reader.conv).toBeNull();
    expect(await reader.locate(terminal(siblingPid, { session: SIBLING, log: log(SIBLING) }))).toBe(true);
    expect(await reader.pull()).toBe(true);
    expect(words(reader)).toEqual(["Sibling only"]);
    expect(await reader.locate(terminal(pid))).toBe(true);
    expect(await reader.pull()).toBe(true);
    expect(words(reader)).toEqual(["Café with Cursor", "Visible answer", "Appended answer"]);
    expect(calls.every(([, machine]) => machine === "somewhere")).toBe(true);

    const saved = new ChatReader(io, "log").fixed(log(ID), "cursor", ID);
    expect(await saved.pull()).toBe(true);
    expect(words(saved)).toEqual(words(reader));
    expect(await saved.locate(terminal(siblingPid))).toBe(false);
    const mismatch = new ChatReader(io, "log").fixed(log(ID), "cursor", SIBLING);
    expect(mismatch.state).toBe("missing");
    expect(await mismatch.pull()).toBe(false);
  });
});

test("a native transcript requires a valid label, an in-root file and one matching live store", async () => {
  await fixture(async ({ io, calls, log, open, terminal, home, root }) => {
    const pid = await open(ID);
    const reader = new ChatReader(io, "t1");
    for (const term of [
      terminal(pid, {}, "{bad"), terminal(pid, {}, "[]"), terminal(pid, { kind: "codex" }),
      terminal(pid, { pid: 100 }), terminal(pid, { session: [ID] }), terminal(pid, { session: SIBLING }),
      terminal(pid, { log: log(ID).replace(ID + ".jsonl", SIBLING + ".jsonl") }),
      terminal(pid, { log: log(ID).replace("/projects/", "/../projects/") }),
      { ...terminal(pid), state: "finished" },
    ]) {
      await reader.locate(term);
      expect(reader.path).toBeNull();
    }
    expect(calls).toHaveLength(0);

    const outside = join(home, "projects", "same worktree", "agent-transcripts", ID + ".jsonl");
    mkdirSync(dirname(outside), { recursive: true });
    writeFileSync(outside, line("assistant", "Outside Cursor root"));
    await reader.locate(terminal(pid, { log: outside }));
    expect(reader.path).toBeNull();
    const ambiguous = await open(ID, SIBLING);
    await reader.locate(terminal(ambiguous));
    expect(reader.path).toBeNull();

    const flat = join(dirname(dirname(log(ID))), ID + ".jsonl");
    symlinkSync(outside, flat);
    await reader.locate(terminal(pid, { log: flat }));
    expect(reader.path).toBeNull();
    const saved = new ChatReader(io, "log").fixed(flat, "cursor");
    expect(await saved.pull()).toBe(true);
    expect(saved.state).toBe("missing");

    const symlinkedProject = join(root, "projects", "symlinked");
    symlinkSync(dirname(dirname(outside)), symlinkedProject);
    await reader.locate(terminal(pid, { log: join(symlinkedProject, "agent-transcripts", ID + ".jsonl") }));
    expect(reader.path).toBeNull();
    rmSync(flat);
    writeFileSync(flat, line("assistant", "Older flat layout"));
    await reader.locate(terminal(pid, { log: flat }));
    await reader.pull();
    expect(words(reader)).toEqual(["Older flat layout"]);
  });
});

test("Cursor ACP messages and approvals stay in their own log format", async () => {
  await fixture(async ({ io, home }) => {
    const path = join(home, "cursor-chats", ID + ".jsonl");
    mkdirSync(dirname(path));
    writeFileSync(path, line("assistant", "Not an ACP event") + [
      { herdr: "cursor", type: "user", text: "ACP question" },
      { herdr: "cursor", type: "agent", text: "ACP answer" },
      { herdr: "cursor", type: "permission", title: "Run tests" },
    ].map((l) => JSON.stringify(l)).join("\n") + "\n");
    const bridge = new ChatReader(io, "log").fixed(path, "cursor", ID);
    await bridge.pull();
    expect(bridge.format).toBeNull();
    expect(words(bridge)).toEqual(["ACP question", "ACP answer"]);
    expect(bridge.conv.doing).toBe("Waiting for you: Run tests");
  });
});

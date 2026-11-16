import { expect, test } from "bun:test";
import { argvFor, canFork, current, forkArgv, FORK_LIVE, isCursorChat, line, liveTaskAgent, menuFor, modelSections, parseDefaults, parsePicks, permAcross, pickKey, resumeArgv, rowFor, sessionOf, withPin, withRecent } from "./agents.js";

const codex = { models: Array.from({ length: 250 }, (_, i) => ({ id: `gpt-${i}`, label: `GPT ${i}`, efforts: ["low", "high"] })) };
const menus = { claude: menuFor("claude", {}), codex: menuFor("codex", codex) };

test("Codex lists every model it has, not its first five", () => {
  expect(menus.codex.models.length).toBe(250);
});

test("nothing typed: pinned, then recent, then the rest of the agent shown", () => {
  const picks = withRecent(withPin(parsePicks(""), pickKey("claude", "haiku")), pickKey("claude", "sonnet"));
  const s = modelSections(menus, "claude", picks, "");
  expect(s.map((x) => x.title)).toEqual(["Pinned", "Recent", "All models"]);
  expect(s[0].rows.map((r) => r.id)).toEqual(["haiku"]);
  expect(s[1].rows.map((r) => r.id)).toEqual(["sonnet"]);
  expect(s[2].rows.some((r) => r.id === "haiku" || r.id === "sonnet")).toBe(false);
});

test("a long list is capped and counts the rest", () => {
  const [all] = modelSections(menus, "codex", null, "");
  expect(all.rows.length).toBe(200);
  expect(all.more).toBe(50);
});

test("typed: every agent's matches, and a row to use the id typed", () => {
  const s = modelSections(menus, "codex", null, "opus 1m");
  expect(s.map((x) => x.key)).toEqual(["claude"]);
  expect(s[0].rows.map((r) => r.id)).toEqual(["opus[1m]"]);
  const t = modelSections(menus, "codex", null, "gpt-7-preview").find((x) => x.key === "typed");
  expect(t.rows.map((r) => [r.agent, r.id, r.typed])).toEqual([["codex", "gpt-7-preview", true], ["claude", "gpt-7-preview", true]]);
  expect(modelSections(menus, "codex", null, "gpt-999").map((x) => x.key)).toEqual(["typed"]);
  expect(modelSections(menus, "codex", null, "gpt-12").find((x) => x.key === "typed").rows.map((r) => r.agent)).toEqual(["claude"]);
});

test("an exact id typed needs no extra row; picks survive bad files", () => {
  expect(parsePicks("not json")).toEqual({ pinned: [], recent: [] });
  expect(withPin(withPin(parsePicks(""), "a:b"), "a:b").pinned).toEqual([]);
});

test("a running model typed by id stays in its agent's list", () => {
  expect(menuFor("claude", {}, "claude-opus-4-8").models[0]).toMatchObject({ id: "claude-opus-4-8", name: "Opus 4.8" });
});

test("a pinned or recent model typed by id stays on offer", () => {
  const picks = withRecent(withPin(parsePicks(""), "claude:claude-opus-4-8"), "codex:gpt-x");
  expect(modelSections(menus, "claude", picks, "")[0].rows[0]).toMatchObject({ id: "claude-opus-4-8", name: "Opus 4.8", pinned: true });
  expect(modelSections(menus, "codex", picks, "")[0]).toMatchObject({ title: "Recent", rows: [{ id: "gpt-x" }] });
  expect(modelSections(menus, "claude", picks, "gpt-x").map((x) => x.key)).toEqual(["codex", "typed"]);
});

// SHEP-41: Fast was hidden (and stuck on) for a Codex whose config.toml set service_tier.
test("every agent with a fast mode shows the Fast switch, and turning it off sticks", () => {
  const cache = JSON.stringify({ models: [{ slug: "gpt-x", display_name: "GPT-X", supported_reasoning_levels: [{ effort: "high" }], service_tiers: [{ id: "priority" }] },
    { slug: "gpt-slow", display_name: "GPT-Slow", service_tiers: [] }] });
  const dflt = parseDefaults("codex", `model\tgpt-x\nservice_tier\tfast\nmodels\t${cache}\n`);
  const codexMenu = menuFor("codex", dflt);
  expect(codexMenu.models.find((m) => m.id === "gpt-x").fast).toBe(true);
  expect(codexMenu.models.find((m) => m.id === "gpt-slow").fast).toBe(false);
  expect(codexMenu.fastLocked).toBeUndefined();
  expect(menuFor("claude", {}).models.find((m) => m.id === "opus").fast).toBe(true);
  // off: said on the command line, so config.toml's fast loses, and the label reads it back
  const off = argvFor({ agent: "codex", model: "gpt-x", effort: "high", fast: false }, { agent: "codex", argv: ["codex", "-c", 'service_tier="fast"'] }).argv;
  expect(off.filter((a) => a.startsWith("service_tier"))).toEqual(['service_tier="default"']);
  expect(current("codex", off, {}, dflt).fast).toBe(false);
  expect(line(current("codex", off, {}, dflt))).toBe("Codex · GPT-X · High");
  const on = argvFor({ agent: "codex", model: "gpt-x", fast: true }, { agent: "codex", argv: off }).argv;
  expect(current("codex", on, {}, dflt).fast).toBe(true);
  const claudeOff = argvFor({ agent: "claude", model: "opus", fast: false }, { agent: "claude", argv: ["claude"] }).argv;
  expect(current("claude", claudeOff, {}, { fast: true }).fast).toBe(false);
});

// A terminal's own argv as herdr-task runs a worker: nice, sh -c, env and the person's `bash -ic` before the agent.
const wrapped = (...agent) => ["sh", "-c", "exec nice -n 10 \"$@\"", "herdr-low", "sh", "-c", "for v in ...; exec \"$@\"", "herdr-clean",
  "env", "HERDR_BROWSER_TASK=demo#1", "/bin/bash", "-ic", "\"$@\"; exec \"$0\" -i", "/bin/bash", ...agent];

test("#1001: a restart keeps only the agent's own flags, never its launcher's (bash -ic was Codex's -i c, an image)", () => {
  const old = { agent: "codex", argv: wrapped("codex", "-c", "hooks.state={}", "--no-daemon", "-m", "gpt-6-sol", "Do the task"), session: "S1" };
  const { argv, base } = argvFor({ agent: "codex", model: "gpt-6-sol", perm: "auto" }, old);
  expect(argv.some((a) => /^-i/.test(a))).toBe(false);
  expect(argv).not.toContain("herdr-clean");
  expect(argv.filter((a, i) => argv[i - 1] === "-c")).toEqual(["hooks.state={}", 'service_tier="default"']);
  expect(base[0]).toBe("codex");
  expect(argv.slice(0, 2)).toEqual(["codex", "resume"]);
  const c = argvFor({ agent: "claude", model: "opus" }, { agent: "claude", argv: wrapped("claude", "--add-dir", "/x", "hi"), session: "S2" }).argv;
  expect(c).toEqual(["claude", "--add-dir", "/x", "--model", "opus", "--settings", '{"fastMode":false}', "--resume", "S2"]);
  expect(forkArgv("codex", wrapped("codex", "-m", "gpt-6-sol"), "S3")).toEqual(["codex", "fork", "-m", "gpt-6-sol", "--no-daemon", "S3"]);
});

test("#973: Fable 5.2 is in Claude's model menu, the fable alias its row", () => {
  const ids = menuFor("claude", {}).models.map((m) => m.id);
  expect(ids).toContain("claude-fable-5-2");
  expect(menuFor("claude", {}).models.find((m) => m.id === "claude-fable-5-2").name).toBe("Fable 5.2");
  expect(rowFor("claude", { alias: "fable", model: "claude-fable-5-2" })).toBe("claude-fable-5-2");
});

test("#1050: forking a task's chat while its agent runs asks first; a plain chat or an ended task doesn't", () => {
  const agent = (address, labels, extra = {}) => ({ address, state: "live", labels, status: { app: "claude-code" }, ...extra });
  const worker = agent("t1", { "herdr.task": "repo#7" });
  const all = [worker, agent("t2", { "herdr.task": "repo#7", "herdr.role": "reviewer" }), agent("t3", { "herdr.task": "repo#8" })];
  expect(liveTaskAgent(worker, all)?.address).toBe("t1");
  // an older session of the task, its agent now t1
  expect(liveTaskAgent({ address: "t0", state: "finished", labels: { "herdr.task": "repo#7" } }, all)?.address).toBe("t1");
  expect(liveTaskAgent(agent("t4", {}), all)).toBe(null);
  // the agent quit, its shell stays
  expect(liveTaskAgent(worker, [{ ...worker, status: { app: null }, foreground: { name: "bash" } }])).toBe(null);
  // only the reviewer left
  expect(liveTaskAgent(worker, [all[1]])).toBe(null);
  expect(FORK_LIVE.verb).toBe("Fork anyway");
});

// Restart Assistant after an /exit (GitHub #1039): the same conversation, with the flags it ran with.
test("an ended Claude Assistant resumes its own session with its own flags", () => {
  const argv = ["claude", "--model", "opus", "--effort", "high", "--dangerously-skip-permissions"];
  expect(resumeArgv("claude", argv, "11111111-2222-3333-4444-555555555555"))
    .toEqual(["claude", "--model", "opus", "--effort", "high", "--dangerously-skip-permissions", "--resume", "11111111-2222-3333-4444-555555555555"]);
});

test("an ended Codex Assistant resumes with codex resume <session>", () => {
  const out = resumeArgv("codex", ["codex", "--no-daemon", "-m", "gpt-5"], "abc");
  expect(out[0]).toBe("codex");
  expect(out[1]).toBe("resume");
  expect(out.at(-1)).toBe("abc");
  expect(out).toContain("--no-daemon");
  expect(out).not.toContain("fork");
});

test("an Assistant that was itself resumed resumes the session it ran last, once", () => {
  expect(resumeArgv("claude", ["claude", "--resume", "old-id", "--model", "opus"], "new-id")).toEqual(["claude", "--model", "opus", "--resume", "new-id"]);
});

// SHEP-2: Cursor Agent as a third agent, never run as Codex or Claude Code.
test("Cursor starts as cursor-agent with its model, --force only for full access, and resumes its chat", () => {
  const ask = argvFor({ agent: "cursor", model: "gpt-5", perm: "manual" }, { agent: "claude", argv: ["claude"] });
  expect(ask).toEqual({ argv: ["cursor-agent", "--model", "gpt-5"], resume: false, base: ["cursor-agent", "--model", "gpt-5"] });
  expect(argvFor({ agent: "cursor", perm: "full" }, { agent: "codex", argv: ["codex"] }).argv).toEqual(["cursor-agent", "--force"]);
  // the same agent: its own flags kept, its model and access chosen again, its chat resumed
  const old = { agent: "cursor", argv: wrapped("agent", "--force", "--model", "gpt-5", "--workspace", "/w", "--resume", "C0", "Do it"), session: "C1" };
  const r = argvFor({ agent: "cursor", model: "sonnet-4", perm: "manual" }, old);
  expect(r.argv).toEqual(["agent", "--workspace", "/w", "--model", "sonnet-4", "--resume", "C1"]);
  expect(r.resume).toBe(true);
  expect(r.base).toEqual(["agent", "--workspace", "/w", "--model", "sonnet-4"]);
  // Codex's or Claude's words never reach it
  expect(r.argv.some((a) => ["--no-daemon", "--settings", "--effort", "-c"].includes(a))).toBe(false);
});

test("Cursor's settings, model menu and running line come from its own config, with no invented efforts", () => {
  const d = parseDefaults("cursor", "model\tgpt-5\nlabel\tOpenAI GPT-5\napproval\t\nsandbox\t\nbin\t/usr/local/bin/cursor-agent\n");
  expect(d).toMatchObject({ model: "gpt-5", label: "OpenAI GPT-5", installed: true });
  expect(parseDefaults("cursor", "bin\t\n")).toMatchObject({ model: null, installed: false, models: [] });
  const menu = menuFor("cursor", d, "sonnet-4-thinking");
  expect(menu.name).toBe("Cursor");
  expect(menu.perms.map((p) => p.id)).toEqual(["manual", "full"]);
  expect(menu.models.map((m) => [m.id, m.efforts, m.fast])).toEqual([["sonnet-4-thinking", [], false], ["gpt-5", [], false]]);
  const c = current("cursor", ["cursor-agent", "--force"], {}, d);
  expect(c).toMatchObject({ agent: "cursor", model: "gpt-5", perm: "full", effort: null, fast: false });
  expect(line(c)).toBe("Cursor · OpenAI GPT-5");
  expect(current("cursor", ["cursor-agent", "--model", "sonnet-4"], {}, d)).toMatchObject({ model: "sonnet-4", perm: "manual" });
  // a model typed by id: no levels made up for it
  const typed = modelSections({ cursor: menu }, "cursor", null, "grok-9").find((x) => x.key === "typed");
  expect(typed.rows[0]).toMatchObject({ agent: "cursor", id: "grok-9", efforts: [] });
});

test("switching to Cursor keeps full access as full access and never grants more than chosen", () => {
  expect(permAcross("bypassPermissions", "cursor", {})).toBe("full");
  expect(permAcross("full", "cursor", {})).toBe("full");
  for (const p of ["auto", "acceptEdits", "plan", "read-only", "manual"]) expect(permAcross(p, "cursor", { cursor: "manual" })).toBe("manual");
  expect(permAcross("full", "claude", {})).toBe("bypassPermissions");
  expect(permAcross("manual", "claude", {})).toBe("manual");
});

test("a Cursor session: its id from its chat or transcript, resumed with --resume; no fork", () => {
  const id = "11111111-2222-3333-4444-555555555555";
  expect(sessionOf("cursor", `/h/.cursor/chats/abc123/${id}/store.db`)).toBe(id);
  expect(sessionOf("cursor", `/h/.cursor/projects/x/agent-transcripts/${id}/${id}.jsonl`)).toBe(id);
  expect(sessionOf("cursor", "/h/.cursor/cli-config.json")).toBe(null);
  expect(resumeArgv("cursor", ["cursor-agent", "--model", "gpt-5", "--resume", "old", "hi"], id)).toEqual(["cursor-agent", "--model", "gpt-5", "--resume", id]);
  expect(forkArgv("cursor", ["cursor-agent"], id)).toBe(null);
  expect(canFork("cursor")).toBe(false);
  expect(canFork("codex")).toBe(true);
});

test("a task's live Cursor worker (no status, running as node) still counts for #1050's fork ask", () => {
  const cursor = { address: "t9", state: "live", labels: { "herdr.task": "repo#3" }, status: null,
    foreground: { name: "node", argv: ["/h/.local/share/cursor-agent/versions/2025.09.12/node", "/h/.local/share/cursor-agent/versions/2025.09.12/index.js"] } };
  expect(liveTaskAgent({ address: "t1", labels: { "herdr.task": "repo#3" } }, [cursor])?.address).toBe("t9");
});

// SHEP-6: Cursor as an Assistant, behind herdr-task's chat bridge (herdr-task cursor-chat).
test("an Assistant runs Cursor through its chat bridge, resumes its chat and keeps the bridge on a restart", () => {
  // a chat's agent picked Cursor: the bridge, never Cursor's own UI the chat can't read
  const fresh = argvFor({ agent: "cursor", model: "composer-2.5", perm: "manual" }, { agent: "claude", argv: ["claude"] }, { chat: true });
  expect(fresh).toEqual({ argv: ["herdr-task", "cursor-chat", "--model", "composer-2.5"], resume: false, base: ["herdr-task", "cursor-chat", "--model", "composer-2.5"] });
  expect(argvFor({ agent: "cursor", perm: "full" }, { agent: "codex", argv: ["codex"] }, { chat: true }).argv).toEqual(["herdr-task", "cursor-chat", "--force"]);
  // already on the bridge (as its process shows it: python running herdr-task): stays on it, resuming its chat
  const old = { agent: "cursor", argv: ["/usr/bin/python3", "/h/.local/bin/herdr-task", "cursor-chat", "--model", "a", "--force"], session: "C1" };
  const r = argvFor({ agent: "cursor", model: "b", perm: "manual" }, old);
  expect(r).toEqual({ argv: ["herdr-task", "cursor-chat", "--model", "b", "--resume", "C1"], resume: true, base: ["herdr-task", "cursor-chat", "--model", "b"] });
  expect(isCursorChat(old.argv)).toBe(true);
  expect(isCursorChat(["cursor-agent", "--force"])).toBe(false);
  // a task's worker stays on Cursor's own UI
  expect(argvFor({ agent: "cursor", perm: "manual" }, { agent: "claude", argv: ["claude"] }).argv).toEqual(["cursor-agent"]);
  // an Assistant that ended resumes the same chat on the bridge (#1039)
  expect(resumeArgv("cursor", ["python3", "/x/herdr-task", "cursor-chat", "--model", "b", "--resume", "C0"], "C2")).toEqual(["herdr-task", "cursor-chat", "--model", "b", "--resume", "C2"]);
  // its transcript names the chat it resumes
  expect(sessionOf("cursor", "/h/.local/share/shepherdr/cursor-chats/11111111-2222-3333-4444-555555555555.jsonl")).toBe("11111111-2222-3333-4444-555555555555");
});

test("Cursor's menu lists the account's models once its chat read them, and says what runs by name", () => {
  const list = JSON.stringify({ models: [{ id: "default[]", name: "Auto" }, { id: "composer-2.5[fast=true]", name: "composer-2.5" }] });
  const d = parseDefaults("cursor", `chatmodels\t${list}\nmodel\tdefault\nbin\t/x/cursor-agent\n`);
  expect(d.models.map((m) => [m.id, m.label])).toEqual([["default[]", "Auto"], ["composer-2.5[fast=true]", "composer-2.5"]]);
  // the bridge's transcript says what runs now, by Cursor's name for it
  const c = current("cursor", ["herdr-task", "cursor-chat"], { model: "composer-2.5[fast=true]", label: "composer-2.5" }, d);
  expect(c).toMatchObject({ agent: "cursor", model: "composer-2.5[fast=true]", label: "composer-2.5", perm: "manual" });
  expect(line(c)).toBe("Cursor · composer-2.5");
  expect(current("cursor", ["herdr-task", "cursor-chat"], { model: "default[]" }, d).label).toBe("Auto");
});

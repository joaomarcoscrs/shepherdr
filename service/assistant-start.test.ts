import { expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, readdirSync, readFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execTemplate } from "./spaces.ts";
import { Agents } from "./agents.ts";

// #1051: a space's Assistant started from the web ran the stock template's `claude`, whatever Settings › Agents chose.
// A hub whose machine.exec runs here, with HOME a throwaway (its templates folder) and PATH only the CLIs given.
function machine(clis: string[], codexHelp: "none" | "root" | "both" = "none") {
  const home = mkdtempSync(join(tmpdir(), "shep-assistant-start-")), bin = join(home, "bin"), dir = join(home, ".config", "herdr-core", "templates");
  mkdirSync(bin); mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "assistant.md"), "---\nname: assistant\ndescription: The Assistant\ncommand: claude\n---\nYou are the Assistant.\n");
  for (const c of clis) {
    const help = c !== "codex" || codexHelp === "none" ? "" :
      `[ "$1" = --help ] || { [ "$1" = resume ] && [ "${codexHelp}" = both ]; } || exit 0\nprintf '  %s\\n' --approve-for-me\n`;
    writeFileSync(join(bin, c), "#!/bin/sh\n" + help);
    chmodSync(join(bin, c), 0o755);
  }
  const targets: (string | undefined)[] = [];
  const hub: any = {
    call: async (_: string, a: { argv: string[]; on?: string }) => {
      targets.push(a.on);
      // hermetic: where agents install system-wide (core/clean-env.js AGENT_PATH) left out, this VM has them there
      const argv = a.argv.map((x) => x.replace(":/opt/homebrew/bin:/usr/local/bin", ""));
      const p = Bun.spawnSync(argv, { env: { HOME: home, PATH: `${bin}:/usr/bin:/bin` } });
      const b64 = (x: Uint8Array) => Buffer.from(x).toString("base64");
      return { code: p.exitCode, stdout: b64(p.stdout), stderr: b64(p.stderr) };
    },
  };
  const made = () => readdirSync(dir).filter((f) => f !== "assistant.md").map((f) => readFileSync(join(dir, f), "utf8"));
  return { hub, made, targets };
}
const commandOf = (file: string) => /^command:(.*)$/m.exec(file)?.[1] ?? "";

test("the Assistant runs the agent its settings choose, with its model, effort and permissions", async () => {
  const { hub, made } = machine(["codex"]);
  const t = await execTemplate(hub, "assistant", null, { agent: "codex", model: "gpt-5.5", effort: "high", fast: false, perm: "full" });
  const [file] = made();
  expect(commandOf(file)).toContain("exec codex --no-daemon -m gpt-5.5");
  expect(commandOf(file)).toContain("--dangerously-bypass-approvals-and-sandbox");
  expect(commandOf(file)).not.toMatch(/exec claude/);
  expect(t.words).toBe("You are the Assistant.");
});

test("no choice: the template's own command, as before", async () => {
  const { hub, made } = machine(["claude"]);
  await execTemplate(hub, "assistant", null, null);
  expect(commandOf(made()[0])).toMatch(/exec claude\s*$/);
});

test("the chosen agent missing there: the template's own; neither there: a plain refusal, never a dead start", async () => {
  const a = machine(["claude"]);
  await execTemplate(a.hub, "assistant", null, { agent: "codex", model: null, effort: null, fast: false, perm: "auto" });
  expect(commandOf(a.made()[0])).toMatch(/exec claude\s*$/);
  const b = machine([]);
  await expect(execTemplate(b.hub, "assistant", null, { agent: "codex", perm: "full" })).rejects.toThrow("Codex isn't installed on this computer");
  expect(b.made()).toEqual([]);
});

test("a fresh Codex Auto Assistant uses the launch host's root and resume capabilities", async () => {
  for (const capability of ["both", "root", "none"] as const) {
    const { hub, made, targets } = machine(["codex"], capability);
    await execTemplate(hub, "assistant", "remote", { agent: "codex", perm: "auto" });
    const command = commandOf(made()[0]);
    if (capability === "both") {
      expect(command).toContain("--approve-for-me");
      expect(command).not.toContain("-s workspace-write");
    } else {
      expect(command).toContain("-s workspace-write -a on-request");
      expect(command).not.toContain("--approve-for-me");
    }
    expect(targets.every((on) => on === "remote")).toBe(true);
  }
});

test("selecting Auto on a legacy Codex session restarts it when its host supports automatic review", async () => {
  const { hub, targets } = machine(["codex"], "both");
  const addr = "remote/t1";
  hub.terminals = new Map([[addr, {
    state: "live", status: { app: "codex", state: "idle" },
    foreground: { name: "codex", argv: ["codex", "-s", "workspace-write", "-a", "on-request"] },
  }]]);
  const agents = new Agents(hub);
  agents.restart = async (address, _reader, pick, supported) => {
    expect(address).toBe(addr);
    expect(pick.perm).toBe("auto");
    expect(supported).toBe(true);
    return "remote/t2";
  };
  expect(await agents.change(addr, null, { kind: "codex", perm: "auto" })).toEqual({ terminal: "remote/t2" });
  expect(targets).toEqual(["remote", "remote"]);
});

test("the web picker offers an Auto review restart only on a supported legacy Auto session", async () => {
  for (const [capability, argv, upgrade] of [
    ["both", ["codex", "-s", "workspace-write"], true],
    ["both", ["codex", "--approve-for-me"], false],
    ["both", ["codex", "-s", "read-only"], false],
    ["root", ["codex", "-s", "workspace-write"], false],
  ] as const) {
    const { hub } = machine(["codex"], capability);
    hub.terminals = new Map([["t1", { state: "live", status: { app: "codex" }, foreground: { name: "codex", argv } }]]);
    expect((await new Agents(hub).menu("t1", null)).codexAutoUpgrade).toBe(upgrade);
  }
});

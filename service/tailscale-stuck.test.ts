// JHQ-40: tailscale's own commands can hang. Each call has a time limit, ends what it started past it, and says so in
// words the person can act on; a stuck read never leads to a share made on a guess. A stand-in tailscale
// (SHEPHERDR_TAILSCALE) hangs, fails or answers, as its mode file says.
import { afterAll, afterEach, beforeEach, describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LIMITS, TailscaleStuck, share, stopTailscale, tailscaleRunning, unshare, whoami } from "./tailscale.ts";

const dir = mkdtempSync(join(tmpdir(), "shep-ts-"));
const fake = join(dir, "tailscale");
// mode: hang (ignores TERM, and starts a child that would outlive it), hang-said (says why, then hangs), fail, ok;
// <mode>.<first word> wins for that command (e.g. hang.serve: only serve hangs)
writeFileSync(fake, `#!/bin/sh
d="${dir}"
echo "$*" >> "$d/calls.log"
mode=$(cat "$d/mode.$1" 2>/dev/null || cat "$d/mode" 2>/dev/null || echo ok)
case "$mode" in
  hang) trap '' TERM; sleep 600 & echo $! > "$d/child.pid"; while :; do sleep 1; done ;;
  hang-said) echo "Serve is not enabled on your tailnet."; echo "To enable, visit: https://login.tailscale.com/f/serve?node=abc"; trap '' TERM; while :; do sleep 1; done ;;
  fail) echo "failed to connect to local tailscaled" >&2; exit 1 ;;
esac
case "$1 $2" in
  "status --json") echo '{"Self":{"UserID":7,"DNSName":"box.tailnet.example.","HostName":"box"},"User":{"7":{"LoginName":"owner@example.com","DisplayName":"Owner"}}}' ;;
  "serve status") cat "$d/served" 2>/dev/null || echo '{}' ;;
  "serve --bg") echo '{"Web":{"box.tailnet.example:443":{"Handlers":{"/shepherdr":{"Proxy":"http://127.0.0.1:7788"}}}}}' > "$d/served" ;;
esac
`, { mode: 0o755 });

const was = { ...LIMITS };
const mode = (m: string, cmd = "") => writeFileSync(join(dir, cmd ? `mode.${cmd}` : "mode"), m);
const calls = () => { try { return readFileSync(join(dir, "calls.log"), "utf8"); } catch { return ""; } };
const alive = (pid: number) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const childPid = () => Number(readFileSync(join(dir, "child.pid"), "utf8").trim());

beforeEach(() => {
  process.env.SHEPHERDR_TAILSCALE = fake;
  Object.assign(LIMITS, { read: 400, change: 400, grace: 200 });
  for (const f of ["mode", "mode.status", "mode.serve", "calls.log", "served", "child.pid"]) rmSync(join(dir, f), { force: true });
});
afterEach(() => { stopTailscale(); Object.assign(LIMITS, was); delete process.env.SHEPHERDR_TAILSCALE; });
afterAll(() => { try { const p = childPid(); if (alive(p)) process.kill(p, "SIGKILL"); } catch {} });

describe("a tailscale that hangs (JHQ-40)", () => {
  test("whoami gives up within its limit and says so plainly", async () => {
    mode("hang");
    const t = Date.now();
    const err = await whoami().then(() => null, (e) => e);
    expect(err).toBeInstanceOf(TailscaleStuck);
    expect(Date.now() - t).toBeLessThan(400 + 2 * 200 + 500);
    expect(err.message).toContain("`tailscale status --json` took over 0 s");
    expect(err.message).toContain("check `tailscale status`");
  });

  test("ends the call and what it started, even one that ignores TERM", async () => {
    mode("hang");
    await whoami().catch(() => {});
    expect(tailscaleRunning()).toBe(0);
    await Bun.sleep(100);
    expect(alive(childPid())).toBe(false);
  });

  test("a stuck serve status shares nothing on a guess (Funnel can't be checked)", async () => {
    mode("hang", "serve");
    const err = await share("box.tailnet.example", 7788).then(() => null, (e) => e);
    expect(err).toBeInstanceOf(TailscaleStuck);
    expect(err.message).toContain("tailscale serve status --json");
    expect(calls()).not.toContain("--bg");
  });

  test("a serve that waits for Serve to be enabled passes on its own words and link", async () => {
    // serve status answers; serve --bg prints why and waits
    writeFileSync(join(dir, "served"), "{}");
    const real = readFileSync(fake, "utf8");
    writeFileSync(fake, real.replace('mode=$(cat "$d/mode.$1"', 'mode=$( [ "$2" = "--bg" ] && echo hang-said || cat "$d/mode.$1"'));
    try {
      const err = await share("box.tailnet.example", 7788).then(() => null, (e) => e);
      expect(err).toBeInstanceOf(TailscaleStuck);
      expect(err.message).toContain("tailscale serve --bg --https=443 --set-path /shepherdr");
      expect(err.message).toContain("Serve is not enabled on your tailnet. To enable, visit: https://login.tailscale.com/f/serve?node=abc");
      expect(tailscaleRunning()).toBe(0);
    } finally {
      writeFileSync(fake, real);
    }
  });

  test("a stuck unshare throws, so it's tried again rather than taken as done", async () => {
    writeFileSync(join(dir, "served"), '{"Web":{"box.tailnet.example:443":{"Handlers":{"/shepherdr":{"Proxy":"http://127.0.0.1:7788"}}}}}');
    const real = readFileSync(fake, "utf8");
    writeFileSync(fake, real.replace('mode=$(cat "$d/mode.$1"', 'mode=$(echo "$*" | grep -q " off$" && echo hang || cat "$d/mode.$1"'));
    try {
      expect(await unshare("box.tailnet.example", 7788).then(() => null, (e) => e)).toBeInstanceOf(TailscaleStuck);
    } finally {
      writeFileSync(fake, real);
    }
  });

  test("the service stopping ends a call still running", async () => {
    mode("hang");
    LIMITS.read = 30_000;
    const look = whoami().then(() => "answered", (e) => e);
    await Bun.sleep(300);
    expect(tailscaleRunning()).toBe(1);
    const pid = childPid();
    stopTailscale();
    expect(await look).toBe("answered"); // ended, not stuck: tailscale's own failure (no owner)
    await Bun.sleep(100);
    expect(tailscaleRunning()).toBe(0);
    expect(alive(pid)).toBe(false);
  });
});

describe("a tailscale that fails or answers", () => {
  test("failing at once: no owner, and serve's own words", async () => {
    mode("fail");
    expect(await whoami()).toBeNull();
    expect(await share("box.tailnet.example", 7788)).toBe("tailscale serve failed: failed to connect to local tailscaled");
  });

  test("answering: the owner, and /shepherdr shared to this service", async () => {
    expect(await whoami()).toEqual({ login: "owner@example.com", name: "Owner", dns: "box.tailnet.example", host: "box" });
    expect(await share("box.tailnet.example", 7788)).toBeNull();
    expect(calls()).toContain("serve --bg --https=443 --set-path /shepherdr 7788");
    expect(existsSync(join(dir, "served"))).toBe(true);
  });

  test("stuck, then answering again: the next look works", async () => {
    mode("hang");
    expect(await whoami().then(() => null, (e) => e)).toBeInstanceOf(TailscaleStuck);
    mode("ok");
    expect((await whoami())?.login).toBe("owner@example.com");
  });
});

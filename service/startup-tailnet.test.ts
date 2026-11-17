// JHQ-40: the real service, started with a tailscale that hangs, listens on its port and writes service.json anyway;
// stopped, it leaves no tailscale call behind. It runs in a throwaway home with stand-ins for every tool it calls
// (hc, herdr-task, herdr-app...: they answer nothing), so nothing of the person's is read or touched.
import { afterAll, expect, test } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const ROOT = join(import.meta.dir, "..");
const box = mkdtempSync(join(tmpdir(), "shep-start-"));
for (const d of ["home", "bin", "data"]) mkdirSync(join(box, d));
const fake = join(box, "bin", "tailscale");
writeFileSync(fake, `#!/bin/sh\necho $$ >> "${box}/tailscale.pids"\ntrap '' TERM\nwhile :; do sleep 1; done\n`, { mode: 0o755 });
for (const t of ["hc", "herdr-secret", "herdr-task", "herdr-artifact", "herdr-browser", "herdr-app"])
  writeFileSync(join(box, "bin", t), `#!/bin/sh\necho "${t}: none here" >&2\nexit 1\n`, { mode: 0o755 });
const port = 40000 + Math.floor(Math.random() * 20000);
const alive = (pid: number) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const pids = () => { try { return readFileSync(join(box, "tailscale.pids"), "utf8").trim().split("\n").filter(Boolean).map(Number); } catch { return []; } };
let svc: ReturnType<typeof Bun.spawn> | null = null;
// a failed run leaves nothing either: the service, and any stand-in tailscale still running from this box
afterAll(async () => {
  svc?.kill("SIGKILL");
  await Bun.sleep(300);
  Bun.spawnSync(["pkill", "-9", "-f", fake]);
});

test("listens and says so with a tailscale that hangs, and leaves none behind when stopped", async () => {
  svc = Bun.spawn([process.execPath, join(ROOT, "service", "main.ts")], {
    env: { HOME: join(box, "home"), PATH: `${join(box, "bin")}:/usr/bin:/bin`, SHEPHERDR_TEST: "1", SHEPHERDR_DATA: join(box, "data"),
      SHEPHERDR_PORT: String(port), SHEPHERDR_HC: join(box, "bin", "hc"), SHEPHERDR_TAILSCALE: fake,
      HERDR_TASK_SETTINGS: join(box, "settings.json"), HERDR_APP_HOME: join(box, "app") },
    stdout: "ignore", stderr: "ignore", stdin: "ignore",
  });
  // health answers within its start (the stand-in hc never comes up: the service waits 10 s for it), while tailscale
  // still hangs
  let health: any = null;
  for (let i = 0; i < 100 && !health; i++) {
    health = await fetch(`http://127.0.0.1:${port}/api/health`).then((r) => r.json()).catch(() => null);
    if (!health) await Bun.sleep(200);
  }
  expect(health?.app).toBe("shepherdr");
  const file = JSON.parse(readFileSync(join(box, "data", "service.json"), "utf8"));
  expect(file.port).toBe(port);
  expect(file.id).toBe(health.id);
  // its first look at the tailnet, started just before, still hangs: one call, not waited for
  for (let i = 0; i < 25 && !pids().length; i++) await Bun.sleep(100);
  expect(pids().filter(alive).length).toBe(1);
  // a tailnet caller isn't let in while who owns the machine isn't known
  const r = await fetch(`http://127.0.0.1:${port}/shepherdr/`, { headers: { "tailscale-user-login": "owner@example.com", host: "box.tailnet.example" } });
  expect(r.status).toBe(503);

  svc.kill("SIGTERM");
  await svc.exited;
  await Bun.sleep(200);
  expect(existsSync(join(box, "data", "service.json"))).toBe(false);
  expect(pids().filter(alive)).toEqual([]);
}, 40_000);

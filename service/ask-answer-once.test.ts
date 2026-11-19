// lib/answers.js's writer, run for real on a throwaway folder (a sleeping process stands in for a waiting hook; no agent
// is asked or answered): an answer goes only while its request waits, never over another one, and how it went with it.
import { afterAll, expect, mock, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fakeHerdr } from "./fake-herdr";

mock.module("herdr", () => fakeHerdr({}));
const { ANSWER_ONCE, askId } = await import("../lib/answers.js");

const hook = Bun.spawn(["sleep", "60"]);
afterAll(() => hook.kill());
const dir = mkdtempSync(join(tmpdir(), "asks-"));
for (const d of ["req", "ans", "end"]) mkdirSync(join(dir, d));
const once = (id: string, how: string, text: string) =>
  Bun.spawnSync(["sh", "-c", ANSWER_ONCE, "x", dir, id, how, text]).exitCode;

test("first answer wins; a second, or one for a request gone or whose hook ended, writes nothing", () => {
  const id = `t7.1763560000.${hook.pid}`;
  writeFileSync(join(dir, "req", `${id}.json`), "{}");
  expect(once(id, "Allowed from a notification", '{"a":1}')).toBe(0);
  expect(readFileSync(join(dir, "ans", `${id}.json`), "utf8")).toBe('{"a":1}');
  expect(readFileSync(join(dir, "end", id), "utf8")).toBe("Allowed from a notification\n");
  expect(once(id, "Denied in the chat", '{"b":2}')).toBe(4);
  expect(readFileSync(join(dir, "ans", `${id}.json`), "utf8")).toBe('{"a":1}');
  expect(readFileSync(join(dir, "end", id), "utf8")).toBe("Allowed from a notification\n");
  // no temp files left behind
  expect(readdirSync(join(dir, "ans"))).toEqual([`${id}.json`]);

  const gone = `t7.1763560001.${hook.pid}`;
  expect(once(gone, "x", "{}")).toBe(3);
  expect(existsSync(join(dir, "ans", `${gone}.json`))).toBe(false);

  const dead = Bun.spawnSync(["sh", "-c", "echo $$"]).stdout.toString().trim();
  const ended = `t7.1763560002.${dead}`;
  writeFileSync(join(dir, "req", `${ended}.json`), "{}");
  expect(once(ended, "x", "{}")).toBe(3);
  expect(existsSync(join(dir, "ans", `${ended}.json`))).toBe(false);
});

test("ids are never paths", () => {
  expect(askId("t7.1763560000.42")).toBe(true);
  for (const bad of ["../x.1.2", "a/b.1.2", "t7", "", "t7..1.2", null]) expect(askId(bad as any)).toBe(false);
});

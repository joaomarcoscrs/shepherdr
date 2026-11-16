// SHEP-6: an Assistant on Cursor (herdr-task cursor-chat) in the web: its real transcript (test/fixtures/cursor-chat-real.jsonl,
// written by the bridge with Cursor 2026.11.13 signed in) through the shared core into the web's chat, and its model menu
// offering Cursor beside Claude Code and Codex.
import { expect, test } from "bun:test";
import { join } from "node:path";
import { Compat } from "./compat.ts";
import { Agents, cursorRole } from "./agents.ts";
import * as CL from "../core/chat-log.js";
const { LOG, Conversation, records } = CL as any;

const FIX = join(import.meta.dir, "..", "test", "fixtures", "cursor-chat-real.jsonl");
const t = { address: "t9", state: "live", status: { app: "cursor", state: "idle" }, cwd: "/work/shop", created_ms: 0,
  foreground: { name: "Python", pid: 1, cwd: "/work/shop", argv: ["python3", "/h/.local/bin/herdr-task", "cursor-chat"] },
  labels: { "herdr.role": "coordinator", "herdr.space": "sp1", "herdr.agent": "cursor" } };

async function reader() {
  const out = await new Response(Bun.spawn(["sh", "-c", LOG, "log", "tail", FIX, "0"], { stdout: "pipe", stderr: "ignore" }).stdout).text();
  const conv = new Conversation("cursor");
  conv.feed(records(out).records);
  return { conv, start: 0, machine: null, addr: "t9", kind: "cursor", path: FIX };
}

test("a Cursor Assistant's conversation is its chat, with what it runs on under the box", async () => {
  const compat = new Compat() as any;
  const chat = compat.chat("p:sp1", await reader(), t);
  expect(chat.cli).toBe("cursor");
  const said = chat.messages.filter((m: any) => m.who === "agent" && m.text).map((m: any) => m.text);
  expect(said).toContain("I read `a.txt`, and it contained “hello”.");
  expect(said).toContain("REFUSED");
  expect(said[said.length - 1]).toBe("OK");
  expect(chat.agent).toMatchObject({ kind: "cursor", words: "composer-2.5" });
  expect(chat.working).toBe(false);
});

test("its model menu offers Cursor beside Claude Code and Codex; a reviewer's doesn't", async () => {
  const hub: any = { terminals: new Map([["t9", t]]), call: async () => ({ code: 0, stdout: Buffer.from("bin\t/x/cursor-agent\n").toString("base64"), stderr: "" }) };
  const menu = await new Agents(hub).menu("t9", await reader());
  expect(Object.keys(menu.menus)).toContain("cursor");
  expect(menu.current).toMatchObject({ agent: "cursor", model: "composer-2.5[fast=true]", perm: "manual" });
  expect(cursorRole(t)).toBe(true);
  expect(cursorRole({ labels: { "herdr.role": "reviewer" } })).toBe(false);
  expect(cursorRole({ labels: { "herdr.role": "helper", "herdr.task": "x#1" } })).toBe(false);
});

import { expect, test } from "bun:test";
import { act } from "./actions.ts";

// SHEP-17: a message to an Assistant on Cursor's chat bridge while it asks a permission once went out as Esc and the
// words, which rejected the permission. Now it waits for the answer, then goes, and nothing presses Esc.
test("words for a Cursor chat that is asking wait for the answer, never Esc it", async () => {
  const sent: string[][] = [];
  const t: any = { state: "live", status: { state: "blocked", app: "cursor" } };
  const hub: any = { terminals: new Map([["t6", t]]), send: async (a: string, text: string) => { sent.push([a, text]); return { code: 0, stdout: "{}", stderr: "" }; } };
  const r: any = await act(hub, { type: "send", addr: "t6", text: "2" }, {} as any);
  expect(r).toMatchObject({ ok: true, waiting: true });
  await Bun.sleep(1300);
  expect(sent).toEqual([]);
  // answered, its turn over: the words go as a message
  t.status = { state: "idle", app: "cursor" };
  await Bun.sleep(1300);
  expect(sent).toEqual([["t6", "2"]]);
});

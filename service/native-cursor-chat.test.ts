import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { act } from "./actions.ts";
import { Compat } from "./compat.ts";
import type { Hub } from "./hub.ts";

test("the task chat cannot type, retry, steer, react or answer in a native Cursor terminal", async () => {
  const send = () => { throw new Error("Unexpected terminal input"); };
  const terminal = { address: "t1", state: "live", labels: { "herdr.agent": "cursor" },
    foreground: { name: "node" }, status: { state: "blocked", msg: "Permission required" } };
  const hub = { terminals: new Map([["t1", terminal]]), send } as unknown as Hub;
  const compat = new Compat();
  const chat = "TEST-1/worker";
  (compat as any).chats.set(chat, "t1");
  for (const status of [terminal.status, { state: "idle", msg: "Unsent terminal draft" }, null]) {
    Object.assign(terminal, { status });
    for (const action of [
      { type: "send", text: "new message" },
      { type: "send", text: "queued message", hold: true },
      { type: "send", text: "retry", cid: "native-cursor-retry" },
      { type: "react", message: "a1", emoji: "👍" },
      { type: "steer" },
      { type: "answer", question: "scr-old", choice: 0 },
      { type: "answer", question: "old-hook-request", dismiss: true },
    ]) {
      await expect(act(hub, { ...action, chat }, compat)).rejects.toThrow("Send messages and answer Cursor in the terminal.");
    }
  }
});

test("a stashed ACP card is hidden from a native Cursor task chat", () => {
  const chat = "TEST-1/worker";
  const compat = new Compat();
  (compat as any).chats.set(chat, "t1");
  (compat as any).asks = { of: () => [{ id: "old-approval", words: { kind: "permission", title: "Old bridge permission" } }] };
  const shown = compat.chat(chat, { kind: "cursor", format: "cursor-native", state: "ready", start: 0 }, { state: "live", status: null });
  expect(shown).toMatchObject({ cli: "cursor", terminalOnly: true, messages: [] });
});

test("the window's task chat keeps existing drafts and held messages out of a native terminal", () => {
  const source = readFileSync(new URL("../mods/chat/main.js", import.meta.url), "utf8");
  const methods = ["send", "deliver", "drainQueue", "sendQueued", "reply", "toggleReaction"].map((name) => {
    const method = new RegExp(`^  ${name}\\(.*?^  \\}`, "ms").exec(source)?.[0];
    expect(method).toBeDefined();
    return method;
  });
  const send = () => { throw new Error("Unexpected terminal input"); };
  const chat = Object.assign(new Function("personHc", `return ({ ${methods.join(",\n")} })`)(send), {
    nativeCursor: () => true, phase: "ready", queue: [{ raw: true, text: "held draft" }],
    box: { value: () => "active draft", set_value: send }, saveSoon: () => {},
  });
  chat.send({});
  chat.drainQueue({});
  chat.sendQueued(chat.queue[0], {});
  chat.reply({ id: "old-approval" }, "allow", {});
  chat.toggleReaction("a1", "message", "👍", {});
  chat.deliver({ text: "queued through the window" }, {});
  expect(chat.box.value()).toBe("active draft");
  expect(chat.queue.map((x: { text: string }) => x.text)).toEqual(["held draft", "queued through the window"]);
});

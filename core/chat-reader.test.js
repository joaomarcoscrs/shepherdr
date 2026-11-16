import { expect, test } from "bun:test";
import { agentKindOf, kindOf } from "./chat-reader.js";

// SHEP-6: Cursor's chat bridge (herdr-task cursor-chat) is a chat agent the chat reads; Cursor's own UI is not.
test("Cursor's chat bridge is a chat the chat reads, by its status or, before one, its command", () => {
  expect(kindOf({ status: { app: "cursor", state: "idle" }, foreground: { name: "python3" } })).toBe("cursor");
  expect(kindOf({ status: null, foreground: { name: "Python", argv: ["/usr/bin/python3", "/h/.local/bin/herdr-task", "cursor-chat", "--model", "x"] } })).toBe("cursor");
  // herdr-task's other commands are no agent
  expect(kindOf({ foreground: { name: "python3", argv: ["python3", "/h/.local/bin/herdr-task", "list"] } })).toBe(null);
  // Cursor's own terminal UI (a task worker's): an agent kind for its menu, but no chat the chat reads
  const tui = { labels: { "herdr.agent": "cursor" }, foreground: { name: "node", argv: ["node", "/x/cursor-agent/index.js"] } };
  expect(kindOf(tui)).toBe(null);
  expect(agentKindOf(tui)).toBe("cursor");
  expect(kindOf({ status: { app: "claude-code" } })).toBe("claude");
  expect(kindOf({ status: { app: "codex" } })).toBe("codex");
});

import { expect, test } from "bun:test";
import { asPerson, personRequest, personResult, sendAsPerson } from "./person.js";
import { refusal } from "./send.js";

// SHEP-3: the window sends the person's words with herdr-core's terminal.send and no caller (hc connect), as the
// service does, so another machine's core never wraps them as <hc from="<machine>/ppid:…">.
function fake(stdout, code = 0) {
  const calls = [];
  return {
    calls,
    io: {
      exec: async (cmd, args) => { calls.push(["exec", cmd, ...args]); return { code, stdout, stderr: "" }; },
      // hc get: not Codex (its mouse-report check, core/mouse-leak.js, has nothing to do)
      hc: async (args) => {
        if (args[0] === "get") return { code: 0, stdout: '{"status":{"app":"claude"}}', stderr: "" };
        calls.push(["hc", ...args]);
        return { code: 0, stdout: '{"started":true}', stderr: "" };
      },
    },
  };
}

test("one terminal.send request, with no caller", () => {
  const req = JSON.parse(personRequest("vm/t10p", 'say "hi"\nthen go'));
  expect(req).toEqual({ jsonrpc: "2.0", id: 1, method: "terminal.send", params: { terminal: "vm/t10p", text: 'say "hi"\nthen go' } });
  expect(personRequest("t1", "a\nb")).not.toContain("\n");
});

test("sent through hc connect, never hc send", async () => {
  const f = fake('{"id":1,"jsonrpc":"2.0","result":{"started":true,"status":"idle","turn":0}}\n');
  const r = await sendAsPerson(f.io, "vm/t5", "hello");
  expect(r.code).toBe(0);
  expect(JSON.parse(r.stdout).started).toBe(true);
  expect(f.calls).toHaveLength(1);
  const [, cmd, , script, , line] = f.calls[0];
  expect(cmd).toBe("sh");
  expect(script).toContain("hc connect");
  expect(JSON.parse(line).params).toEqual({ terminal: "vm/t5", text: "hello" });
});

test("core's refusal reads as hc send's", async () => {
  const f = fake('{"error":{"code":-32000,"data":{"kind":"blocked"},"message":"it waits on its own dialog"},"id":1,"jsonrpc":"2.0"}\n');
  const r = await sendAsPerson(f.io, "t5", "hello");
  expect(r.code).not.toBe(0);
  expect(refusal(r)).toEqual({ kind: "blocked", message: "it waits on its own dialog" });
});

test("no answer from hc connect: hc send still sends the words", async () => {
  const f = fake("", 127);
  const r = await sendAsPerson(f.io, "t5", "hello");
  expect(r.code).toBe(0);
  expect(f.calls.map((c) => c[0])).toEqual(["exec", "hc"]);
  expect(personResult({ stdout: "not json" })).toBeNull();
});

test("asPerson: only a plain send goes as the person", async () => {
  const f = fake('{"id":1,"jsonrpc":"2.0","result":{}}');
  const h = asPerson(f.io);
  await h(["send", "t5", "hi"]);
  await h(["keys", "t5", "esc"]);
  await h(["send", "t5", "hi", "--force"]);
  expect(f.calls.map((c) => c[0] === "exec" ? "connect" : c[1])).toEqual(["connect", "keys", "send"]);
});

// SHEP-13: to Codex, mouse reports it typed into its box (alone there) are deleted before the words go
test("to Codex: reports alone in its box are deleted first", async () => {
  let box = "[<64;101;61M[<65;101;61M";
  const calls = [];
  const io = {
    exec: async (cmd, args) => { calls.push(["send", box + JSON.parse(args[3]).params.text]); return { code: 0, stdout: '{"id":1,"jsonrpc":"2.0","result":{}}', stderr: "" }; },
    hc: async (args) => {
      if (args[0] === "get") return { code: 0, stdout: '{"status":{"app":"codex"}}' };
      if (args[0] === "read") return { code: 0, stdout: `› ${box}\n\n  gpt · ~\n` };
      calls.push(args.slice(2));
      for (const k of args.slice(2)) if (k === "backspace") box = box.slice(0, -1);
      return { code: 0, stdout: "{}" };
    },
  };
  await sendAsPerson(io, "t5", "/model");
  expect(calls[0]).toEqual(["f12"]);
  expect(calls.at(-1)).toEqual(["send", "/model"]);
});

test("to Codex: a draft with reports in it is left as it is", async () => {
  const box = "fix it[<65;101;61M";
  const keys = [];
  const io = {
    exec: async () => ({ code: 0, stdout: '{"id":1,"jsonrpc":"2.0","result":{}}', stderr: "" }),
    hc: async (args) => {
      if (args[0] === "get") return { code: 0, stdout: '{"status":{"app":"codex"}}' };
      if (args[0] === "read") return { code: 0, stdout: `› ${box}\n\n  gpt · ~\n` };
      keys.push(...args.slice(2));
      return { code: 0, stdout: "{}" };
    },
  };
  await sendAsPerson(io, "t5", "hello");
  expect(keys).toEqual(["f12"]);
});

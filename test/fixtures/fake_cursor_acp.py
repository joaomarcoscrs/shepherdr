#!/usr/bin/env python3
"""A stand-in for `cursor-agent acp` (Cursor's ACP server) in the cursor-chat tests: the messages the real one sent in
SHEP-6's probe (2026.11.13), shortened. Its argv goes to $FAKE_ACP_LOG as {"argv": ...}, each request it gets too.

Prompts it knows: "say <words>" answers the words; "run <cmd>" calls its shell tool, asking permission first (its
allowlist is empty), and runs nothing; "hang" works until cancelled. A session/load of "gone" fails; any other
replays one exchange. FAKE_ACP_SIGNED_OUT=1: session/new fails as Cursor does signed out."""
import json
import os
import re
import sys
import threading

LOG = os.environ.get("FAKE_ACP_LOG")
lock = threading.Lock()
pending = {}
cancelled = threading.Event()
MODELS = [{"modelId": "default[]", "name": "Auto"}, {"modelId": "composer-2.5[fast=true]", "name": "composer-2.5"},
          {"modelId": "claude-opus-5-5[context=300k,effort=medium,fast=false]", "name": "claude-opus-5-5"}]
current = {"model": "default[]"}


def log(rec):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")


def send(m):
    with lock:
        sys.stdout.write(json.dumps(m) + "\n")
        sys.stdout.flush()


def update(sid, u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def opts():
    return [{"id": "model", "type": "select", "currentValue": current["model"], "options": [{"value": m["modelId"], "name": m["name"]} for m in MODELS]}]


def prompt(i, p):
    sid = p["sessionId"]
    # the words without the tag hc wraps a message from another agent in (<hc from="t3">...</hc>)
    text = re.sub(r"^<hc[^>]*>|</hc>$", "", p["prompt"][0]["text"].strip())
    if text.startswith("say "):
        for w in text[4:].split(" "):
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": w + " "}})
    elif text.startswith("run "):
        cmd = text[4:]
        tid = "call-1\nfc_1"
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": tid, "title": f"`{cmd}`", "kind": "execute", "status": "pending", "rawInput": {"command": cmd}})
        ev = threading.Event()
        pending[0] = ev
        send({"jsonrpc": "2.0", "id": 0, "method": "session/request_permission", "params": {"sessionId": sid, "toolCall": {
            "toolCallId": tid, "title": f"`{cmd}`", "kind": "execute", "status": "pending",
            "content": [{"type": "content", "content": {"type": "text", "text": f"Not in allowlist: {cmd.split()[0]}"}}]},
            "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]}})
        ev.wait(30)
        picked = pending.pop("answer", {})
        ok = picked.get("optionId", "").startswith("allow")
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": "completed" if ok else "failed",
                     **({"rawOutput": {"exitCode": 0, "stdout": "ran\n", "stderr": ""}} if ok else {})})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ALLOWED" if ok else "REFUSED"}})
    elif text == "hang":
        update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
        cancelled.wait(30)
        cancelled.clear()
        send({"jsonrpc": "2.0", "id": i, "result": {"stopReason": "cancelled"}})
        return
    send({"jsonrpc": "2.0", "id": i, "result": {"stopReason": "end_turn"}})


def main():
    log({"argv": sys.argv[1:]})
    for line in sys.stdin:
        m = json.loads(line)
        log({"got": m})
        meth, i, p = m.get("method"), m.get("id"), m.get("params") or {}
        if meth is None and i in (0,):
            pending["answer"] = (m.get("result") or {}).get("outcome") or {}
            pending[0].set()
        elif meth == "initialize":
            send({"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, "authMethods": [{"id": "cursor_login"}]}})
        elif meth == "session/new":
            if os.environ.get("FAKE_ACP_SIGNED_OUT"):
                send({"jsonrpc": "2.0", "id": i, "error": {"code": -32000, "message": "Authentication required"}})
            else:
                send({"jsonrpc": "2.0", "id": i, "result": {"sessionId": "11111111-2222-3333-4444-555555555555",
                     "models": {"currentModelId": current["model"], "availableModels": MODELS}, "configOptions": opts()}})
        elif meth == "session/load":
            if p.get("sessionId") == "gone":
                send({"jsonrpc": "2.0", "id": i, "error": {"code": -32002, "message": "Session not found"}})
            else:
                update(p["sessionId"], {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "earlier"}})
                update(p["sessionId"], {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "EARLIER"}})
                send({"jsonrpc": "2.0", "id": i, "result": {"models": {"currentModelId": current["model"], "availableModels": MODELS}, "configOptions": opts()}})
        elif meth == "session/set_config_option":
            if not any(x["modelId"] == p.get("value") for x in MODELS):
                send({"jsonrpc": "2.0", "id": i, "error": {"code": -32602, "message": "Unknown model"}})
            else:
                current["model"] = p["value"]
                # as Cursor does: the pick saved as the default for new sessions in cli-config.json
                cfg = os.path.join(os.environ.get("CURSOR_CONFIG_DIR") or os.path.expanduser("~/.cursor"), "cli-config.json")
                if os.path.exists(cfg):
                    with open(cfg) as f:
                        d = json.load(f)
                    d["model"] = {"modelId": p["value"].split("[")[0]}
                    d["selectedModel"] = {"modelId": p["value"].split("[")[0]}
                    d["modelSelectionHistory"] = [p["value"].split("[")[0]] + d.get("modelSelectionHistory", [])
                    with open(cfg, "w") as f:
                        json.dump(d, f, indent=2)
                send({"jsonrpc": "2.0", "id": i, "result": {"configOptions": opts()}})
        elif meth == "session/prompt":
            threading.Thread(target=prompt, args=(i, p), daemon=True).start()
        elif meth == "session/cancel":
            cancelled.set()


if __name__ == "__main__":
    main()

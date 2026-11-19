#!/usr/bin/env python3
"""A stand-in for `cursor-agent acp` (Cursor's ACP server) in the cursor-chat tests: the messages the real one sent in
SHEP-6's probe (2026.11.13), shortened. Its argv goes to $FAKE_ACP_LOG as {"argv": ...}, each request it gets too.

Prompts it knows: "say <words>" answers the words; "run <cmd>" calls its shell tool, asking permission first (its
allowlist is empty), and runs nothing; "both <cmd>" asks for a web fetch and for <cmd> at once, before either is
answered (as the real one runs tool calls in parallel: SHEP-17), and says how each went; "drop <cmd>" asks, then ends
its turn without waiting (a request Cursor gave up on); "plain <cmd>" asks offering no Allow always, "noreason <cmd>" with
no "Not in allowlist" line; "hang" works until cancelled. An answer picking Allow always saves what cursor-agent
2026.11.17 saves (its ACP persistAllowlistEntry, requestWebPermission) to $FAKE_ACP_ALLOW, a stand-in for
~/.cursor/cli-config.json's permissions.allow: one "Kind(value)" a line, never the person's own config (SHEP-18). A session/load of "gone" fails; any other
replays one exchange. FAKE_ACP_SIGNED_OUT=1: session/new fails as Cursor does signed out."""
import json
import os
import re
import sys
import threading

LOG = os.environ.get("FAKE_ACP_LOG")
ALLOW = os.environ.get("FAKE_ACP_ALLOW")
tools = {}                 # request id -> (its toolCall, the shell commands its allowlist lacks)
lock = threading.Lock()
pending = {}               # request id -> Event, set once the client answers it
answers = {}               # request id -> the outcome the client sent
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


OPTIONS = [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
           {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
           {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]


def ask(sid, rid, tool, options=OPTIONS, lacks=None):
    """A session/request_permission with id rid; -> its threading.Event (set once answered: answers[rid])."""
    ev = threading.Event()
    pending[rid] = ev
    tools[rid] = (tool, lacks)
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {"sessionId": sid, "toolCall": tool,
          "options": options}})
    return ev


def persist(rid, outcome):
    """What Cursor saves on Allow always ("allow-always" picked), as cursor-agent 2026.11.17 does: each shell command
    its allowlist lacked (notAllowedCommands), the host of a web fetch; nothing for any other answer."""
    tool, lacks = tools.pop(rid, (None, None))
    if not ALLOW or not tool or outcome.get("optionId") != "allow-always":
        return
    if tool.get("kind") == "execute":
        entries = [f"Shell({c})" for c in (lacks if lacks is not None else [tool["title"].strip("`")])]
    elif tool.get("kind") == "fetch":
        entries = ["WebFetch(" + re.sub(r"^https?://([^/:]+).*$", r"\1", tool["title"][len("Fetch "):]).lower() + ")"]
    else:
        entries = []
    with open(ALLOW, "a") as f:
        for e in entries:
            f.write(e + "\n")


def shell_tool(tid, cmd):
    return {"toolCallId": tid, "title": f"`{cmd}`", "kind": "execute", "status": "pending",
            "content": [{"type": "content", "content": {"type": "text", "text": f"Not in allowlist: {cmd.split()[0]}"}}]}


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
        ask(sid, 0, shell_tool(tid, cmd), lacks=[cmd.split()[0]]).wait(30)
        picked = answers.pop(0, {})
        ok = picked.get("optionId", "").startswith("allow")
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": "completed" if ok else "failed",
                     **({"rawOutput": {"exitCode": 0, "stdout": "ran\n", "stderr": ""}} if ok else {})})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ALLOWED" if ok else "REFUSED"}})
    elif text.startswith("both "):
        cmd = text[5:]
        url = "https://example.com/thread"
        web = {"toolCallId": "web_fetch_1", "title": f"Fetch {url}", "kind": "fetch", "status": "pending"}
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "web_fetch_1", "title": f"Fetch {url}", "kind": "fetch", "status": "pending", "rawInput": {"url": url}})
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "call-2", "title": f"`{cmd}`", "kind": "execute", "status": "pending", "rawInput": {"command": cmd}})
        # both asked before either is answered, each under its own request id
        evs = [ask(sid, 100, web), ask(sid, 101, shell_tool("call-2", cmd), lacks=[cmd.split()[0]])]
        for ev in evs:
            ev.wait(30)
        said = []
        for rid, name in ((100, "FETCH"), (101, "SHELL")):
            o = answers.pop(rid, {})
            said.append(f"{name}:{o.get('optionId') or o.get('outcome') or 'none'}")
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " ".join(said)}})
    elif text.startswith("plain ") or text.startswith("noreason "):
        word, cmd = text.split(" ", 1)
        tool = shell_tool("call-4", cmd)
        if word == "noreason":
            tool.pop("content")
        ask(sid, 300, tool, options=[o for o in OPTIONS if word != "plain" or o["kind"] != "allow_always"],
            lacks=[] if word == "noreason" else [cmd.split()[0]]).wait(30)
        o = answers.pop(300, {})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"GOT:{o.get('optionId') or o.get('outcome')}"}})
    elif text.startswith("drop "):
        cmd = text[5:]
        ask(sid, 200, shell_tool("call-3", cmd))
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "GAVE UP"}})
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
        if meth is None and i in pending:
            answers[i] = (m.get("result") or {}).get("outcome") or {}
            persist(i, answers[i])
            pending.pop(i).set()
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

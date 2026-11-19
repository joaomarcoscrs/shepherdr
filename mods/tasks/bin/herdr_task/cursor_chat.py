"""`herdr-task cursor-chat`: Cursor Agent as a chat agent (an Assistant), in its terminal (SHEP-6).

Cursor's own terminal UI keeps no log Shepherdr can read and reports no status, so an Assistant on Cursor runs this
instead: it starts one `cursor-agent acp` (Cursor's Agent Client Protocol server: JSON-RPC over stdio) and is the
client a person or the chat types into, as they would into Claude Code:

- What is typed (or pasted by `hc send`, the chat's way in) goes to Cursor as a prompt; a message sent mid-turn
  waits its turn (queued, as Claude Code's queue). Esc or Ctrl+C stops the turn (session/cancel).
- What Cursor says and does is shown here and kept in a transcript (CHATS/<chat id>.jsonl, held open while it runs:
  the chat finds it by this process, core/chat-log.js FIND) that the window's chat and the web app read.
- Its status goes to Herdr as OSC 7501 (app=cursor): idle, working, blocked (a permission), error.
- A permission Cursor asks for (session/request_permission: what its allowlist lacks) is shown here as a menu, and
  goes to the chat as a card, the same files Claude Code's PermissionRequest hook leaves (mods/chat/ask-hook.sh:
  data/chat-asks/req/<id>.json, answered in ans/<id>.json). Whichever answers first wins; nothing is allowed unread.
  Several can wait at once (Cursor runs tool calls side by side): each has its own card and is answered under its own
  request id. Only a key pressed on its own answers the menu (a number; Esc or Ctrl+C rejects): a paste, Enter alone,
  a mouse or focus report never does, and words typed or sent meanwhile wait their turn (SHEP-17).
- `--resume <chat id>` loads that chat again (session/load: the conversation goes on); `--model` picks a model and
  `/model <id>` switches it in place; `--force` is Full access (Cursor's own: its deny list still applies).
- No Auto: Cursor's ACP server ignores Auto-review (`cursor-agent --auto-review acp` and cli-config.json's approvalMode
  "auto-review" still ask for what its terminal UI runs: cursor-agent 2026.11.13, SHEP-9). `--auto-review` given here
  runs as Ask and says so, never passed on as if it worked.

Usage: herdr-task cursor-chat [--model <id>] [--force] [--resume <chat id>] [first message...]
"""

import base64
import json
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import layout

NAME = "cursor"           # what its status reports as its app (core/chat-reader.js KINDS)
CLIP = 4000               # a tool's input or output kept in the transcript, at most this many characters
# Cursor's ACP server never turns Auto-review's classifier on (its sessions send no smartModeClassifierAutoModeEnabled,
# whatever --auto-review or approvalMode say), so this chat asks for every call its allowlist lacks.
AUTO_IGNORED = "Auto-review isn't available in a Cursor chat (Cursor's ACP server ignores it): it runs in Ask, asking you for what its allowlist lacks."
ASK_WAIT = 1800           # a permission waits this long for an answer (as the hook's 30 minutes)


def chats_dir():
    return os.environ.get("HERDR_CURSOR_CHATS") or layout.Where().path("cursor-chats")


def cursor_bin():
    """Cursor Agent's CLI: $HERDR_CURSOR_BIN, else cursor-agent or agent on PATH."""
    own = os.environ.get("HERDR_CURSOR_BIN")
    if own:
        return own
    return shutil.which("cursor-agent") or shutil.which("agent")


# What Cursor saves as the default for new sessions when a session's model changes (session/set_config_option writes
# them to cli-config.json, as its own /model does; seen in SHEP-6): put back afterwards, so a model picked for one chat
# never changes the person's Cursor default (as core/live-switch.js puts Claude Code's and Codex's back).
MODEL_KEYS = ("model", "selectedModel", "modelParameters", "modelSelectionHistory", "hasChangedDefaultModel")


def cursor_config():
    d = os.environ.get("CURSOR_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".cursor")
    f = os.path.join(d, "cli-config.json")
    if not os.path.exists(f):
        alt = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "cursor", "cli-config.json")
        if os.path.exists(alt):
            return alt
    return f


def read_config(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def restore_model_keys(path, before):
    """cli-config.json with MODEL_KEYS as they were in `before` (only those keys: anything else written meanwhile
    stays). -> whether it wrote."""
    now = read_config(path)
    if before is None or now is None:
        return False
    changed = False
    for k in MODEL_KEYS:
        if json.dumps(before.get(k), sort_keys=True) == json.dumps(now.get(k), sort_keys=True):
            continue
        changed = True
        if k in before:
            now[k] = before[k]
        else:
            now.pop(k, None)
    if not changed:
        return False
    tmp = f"{path}.herdr-{os.getpid()}.tmp"
    try:
        mode = os.stat(path).st_mode & 0o777
        with open(tmp, "w") as f:
            f.write(json.dumps(now, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def asks_dir():
    return os.path.join(os.environ.get("HERDR_APP_HOME") or os.path.join(os.path.expanduser("~"), ".config", "herdr-app"),
                        "data", "chat-asks")


def now_iso():
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t % 1) * 1000)


def clip(s, n=CLIP):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def clipped(v, n=CLIP):
    """A JSON value with every string cut to n characters (a transcript line stays small)."""
    if isinstance(v, str):
        return clip(v, n)
    if isinstance(v, list):
        return [clipped(x, n) for x in v[:50]]
    if isinstance(v, dict):
        return {str(k): clipped(x, n) for k, x in list(v.items())[:50]}
    return v


def osc(state, msg=None, kind=None):
    """Herdr's status report (OSC 7501), as Claude Code writes it: key=value pairs, words base64."""
    parts = [f"state={state}", f"app={NAME}"]
    if state == "blocked" and kind:
        parts.append(f"kind={kind}")
    if msg:
        parts.append("msg=" + base64.b64encode(msg[:2048].encode()).decode())
    return "\x1b]7501;" + ":".join(parts) + "\x07"


# ---- what Cursor's tool calls are, in the words the chat's cards and rows use --------------------------------------

def tool_path(inp, locations=None):
    for k in ("path", "file_path", "filePath", "target_file", "targetFile", "file"):
        if isinstance(inp, dict) and isinstance(inp.get(k), str):
            return inp[k]
    for loc in locations or []:
        if isinstance(loc, dict) and isinstance(loc.get("path"), str):
            return loc["path"]
    return None


def call_id(v):
    """A tool call's id as one plain word (Cursor's hold a newline): what the chat's records join calls and results by."""
    return re.sub(r"[^\w.:-]+", "_", str(v or "")) or "call"


def strip_ticks(title):
    t = str(title or "").strip()
    return t[1:-1] if len(t) > 1 and t.startswith("`") and t.endswith("`") else t


def hook_request(tool, cwd, session):
    """A permission as Claude Code's PermissionRequest hook gives it (core/asks.js words() reads it): the chat's card
    shows and answers it as any other agent's."""
    kind = tool.get("kind") or "other"
    inp = tool.get("rawInput") if isinstance(tool.get("rawInput"), dict) else {}
    title = strip_ticks(tool.get("title"))
    why = " ".join(c.get("content", {}).get("text", "") for c in tool.get("content") or []
                   if isinstance(c, dict) and isinstance(c.get("content"), dict)).strip()
    diff = next((c for c in tool.get("content") or [] if isinstance(c, dict) and c.get("type") == "diff"), None)
    path = tool_path(inp, tool.get("locations"))
    if kind == "execute":
        name, ti = "Bash", {"command": inp.get("command") or title, **({"description": why} if why else {})}
    elif kind in ("edit", "delete", "move") and (diff or path):
        if diff and diff.get("oldText"):
            name, ti = "Edit", {"file_path": diff.get("path") or path, "old_string": diff.get("oldText") or "", "new_string": diff.get("newText") or ""}
        else:
            name, ti = "Write", {"file_path": (diff or {}).get("path") or path, "content": (diff or {}).get("newText") or inp.get("content") or ""}
    elif kind == "read" and path:
        name, ti = "Read", {"file_path": path}
    elif kind == "fetch" and (inp.get("url") or re.search(r"https?://\S+", title)):
        # Cursor's own web fetch asks with only a title, "Fetch <url>" (cursor-agent 2026.11.17 requestWebPermission)
        name, ti = "WebFetch", {"url": inp.get("url") or re.search(r"https?://\S+", title).group(0)}
    elif kind == "search" and (inp.get("pattern") or inp.get("query")):
        name, ti = "Grep", {"pattern": inp.get("pattern") or inp.get("query"), **({"path": path} if path else {})}
    else:
        name, ti = title or "a tool", {**inp, **({"why": why} if why else {})}
    return {"session_id": session, "cwd": cwd, "hook_event_name": "PermissionRequest", "tool_name": name,
            "tool_input": clipped(ti, 20000), "herdr_agent": NAME}


def report_msg(req):
    """What it waits on, as Claude Code reports it ("approve Bash: npm test": core/needs.js reportReason)."""
    v = req.get("tool_input") or {}
    what = v.get("command") or v.get("file_path") or v.get("url") or v.get("pattern") or ""
    return clip(f"approve {req['tool_name']}: {str(what).splitlines()[0] if what else ''}".strip(), 300)


# What the chat's card sends when the person writes in the chat instead of using it (mods/chat/cards.js replyWith).
# Cursor's reject carries no words, so the bridge sends them as the person's next message.
WROTE_INSTEAD = "The person didn't use the card and wrote in the chat instead: "


def always_scope(ask):
    """What Allow always adds to Cursor's allowlist, in words: Cursor saves each command its allowlist lacks ("Not in
    allowlist: gh, jq" -> Shell(gh), Shell(jq): every gh command from then on), a path for a write or a read, a site
    for a fetch (cursor-agent 2026.11.17's persistAllowlistEntry)."""
    m = re.search(r"Not in allowlist:\s*([^\n]+)", ask.get("why") or "")
    if ask.get("kind") == "execute" and m:
        cmds = [c.strip() for c in m.group(1).split(",") if c.strip()]
        return "adds " + ", ".join(f"Shell({c})" for c in cmds) + " to Cursor's allowlist, for every such command"
    return "Cursor saves it in its allowlist and stops asking for it"


def how_in_terminal(o):
    """A card's quiet line when the terminal answered it."""
    k = str(o.get("kind") or "")
    return "Allowed always in the terminal" if k == "allow_always" else "Allowed once in the terminal" if k.startswith("allow") else "Rejected in the terminal"


def option_for(options, kinds):
    """The option of the first of `kinds` Cursor offers (allow_once, allow_always, reject_once, reject_always)."""
    for k in kinds:
        for o in options or []:
            if o.get("kind") == k:
                return o
    return None


# ---- the transcript ------------------------------------------------------------------------------------------------

class Transcript:
    """CHATS/<chat id>.jsonl: one JSON record a line, each starting {"herdr":"cursor",...} (core/chat-log.js LOG keeps
    them). Held open while this runs, so the chat finds it by this process (FIND)."""

    def __init__(self, folder, chat):
        os.makedirs(folder, mode=0o700, exist_ok=True)
        self.path = os.path.join(folder, f"{chat}.jsonl")
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.f = os.fdopen(fd, "a", encoding="utf-8")

    def write(self, typ, **fields):
        rec = {"herdr": NAME, "type": typ, "at": now_iso(), **fields}
        # compact, as core/chat-log.js LOG finds them ({"herdr":"cursor",...})
        self.f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.f.flush()

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


# ---- the bridge ----------------------------------------------------------------------------------------------------

class Bridge:
    def __init__(self, argv, out=None, inp=None, env=None):
        self.env = os.environ if env is None else env
        self.out = out or sys.stdout
        self.inp = inp if inp is not None else sys.stdin
        self.model = None
        self.force = False
        self.auto_asked = False
        self.resume = None
        words = []
        it = iter(argv)
        for a in it:
            if a == "--model":
                self.model = next(it, None)
            elif a.startswith("--model="):
                self.model = a.split("=", 1)[1]
            elif a in ("--force", "-f", "--yolo"):
                self.force = True
            elif a == "--auto-review":
                self.auto_asked = True   # Cursor's ACP server ignores it: Ask, said so (AUTO_IGNORED)
            elif a == "--resume":
                self.resume = next(it, None)
            elif a.startswith("--resume="):
                self.resume = a.split("=", 1)[1]
            elif a.startswith("-") and not words:
                continue   # a flag it doesn't take (another version's): never a first message
            else:
                words.append(a)
        self.first = " ".join(words).strip() or None
        self.cwd = os.getcwd()
        self.events = queue.Queue()
        self.proc = None
        self.next_id = 0
        self.waiting = {}        # request id -> what its answer is for
        self.session = None
        self.models = []         # [{id, name}] as Cursor lists them for this account
        self.model_id = None     # the model value Cursor runs now
        self.transcript = None
        self.busy = False        # a prompt is running
        self.queue = []          # messages typed while busy
        self.text = ""           # Cursor's words since the last flush
        self.asks = []           # the permissions waiting now, oldest first: [{rpc, options, req, title, why, kind, at, id}]
        self.ask_sec = 0         # the last second a card's id was named by (leave_ask)
        self.buffer = ""         # what is typed in the box
        self.pasting = False
        self.ctrl_c = 0
        self.loading = False     # session/load replays the chat: not written again
        self.err_tail = []
        self.done = False
        self.code = 0
        self.tty = hasattr(self.inp, "isatty") and self.inp.isatty()
        self.eof = False
        self.lost = None
        self.setting = False     # a model change waits for Cursor's answer
        self.cfg = None          # Cursor's cli-config.json and its model keys before a change (restore_model_keys)
        self.cfg_before = None
        self.restores = []       # when to put them back

    # ---- output ----

    def emit(self, s):
        try:
            self.out.write(s)
            self.out.flush()
        except (OSError, ValueError):
            pass

    def status(self, state, msg=None, kind=None):
        self.emit(osc(state, msg, kind))

    def say(self, s):
        """A line on the screen, above the box."""
        self.emit("\r\x1b[K" + s.replace("\n", "\r\n" if self.tty else "\n") + ("\r\n" if self.tty else "\n"))
        self.box()

    def box(self):
        if not self.tty:
            return
        # one screen line at most: a longer one wraps, and the next redraw (which clears only its own line) would
        # leave the wrapped part behind, once per key
        shown = self.buffer.replace("\n", " ⏎ ")
        room = max(10, shutil.get_terminal_size((100, 24)).columns - 3)
        if len(shown) > room:
            shown = "…" + shown[-(room - 1):]
        self.emit("\r\x1b[K\x1b[2m›\x1b[0m " + shown)

    # ---- Cursor's process ----

    def send(self, msg):
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            pass

    def call(self, method, params, why):
        self.next_id += 1
        self.waiting[self.next_id] = why
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return self.next_id

    def start_agent(self):
        exe = cursor_bin()
        if not exe:
            self.fail("Cursor Agent isn't installed here (no cursor-agent or agent on PATH).")
            return False
        cmd = [exe] + (["--force"] if self.force else []) + ["acp"]
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, bufsize=1, env=dict(self.env))
        except OSError as e:
            self.fail(f"Cursor Agent didn't start: {e}")
            return False
        threading.Thread(target=self.read_agent, daemon=True).start()
        threading.Thread(target=self.read_errors, daemon=True).start()
        self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                                             "terminal": False}}, "init")
        return True

    def read_agent(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.events.put(("acp", json.loads(line)))
            except ValueError:
                continue
        self.events.put(("exit", self.proc.wait()))

    def read_errors(self):
        for line in self.proc.stderr:
            line = line.strip()
            if line:
                self.err_tail = (self.err_tail + [clip(line, 300)])[-5:]

    # ---- the session ----

    def on_response(self, m):
        why = self.waiting.pop(m.get("id"), None)
        err = m.get("error")
        res = m.get("result") or {}
        if why == "init":
            if err:
                return self.fail(f"Cursor didn't start its session: {err.get('message', err)}")
            if self.resume:
                self.loading = True
                self.call("session/load", {"sessionId": self.resume, "cwd": self.cwd, "mcpServers": []}, "load")
            else:
                self.call("session/new", {"cwd": self.cwd, "mcpServers": []}, "new")
        elif why in ("new", "load"):
            self.loading = False
            if err:
                msg = str(err.get("message") or err)
                if re.search(r"auth|log ?in|sign", msg, re.I):
                    self.status("blocked", "Cursor is signed out: run cursor-agent login", "auth")
                    return self.fail("Cursor is signed out here. Sign in with `cursor-agent login` in a terminal, then start this again.")
                if why == "load":
                    # its chat is gone: a new one, said so in the chat and here (the old transcript stays on disk)
                    old = self.resume
                    self.resume = None
                    self.call("session/new", {"cwd": self.cwd, "mcpServers": []}, "new")
                    self.lost = f"Cursor couldn't load chat {old} ({clip(msg, 200)}): this is a new chat. The old one's words are in {os.path.join(chats_dir(), old + '.jsonl')}."
                    return
                return self.fail(f"Cursor didn't start its session: {clip(msg, 300)}")
            self.ready(res, resumed=why == "load")
        elif why and why.startswith("model:"):
            want = why.split(":", 1)[1]
            self.setting = False
            # now, and twice more in case Cursor saves after it answers
            self.restores = [time.time(), time.time() + 1.5, time.time() + 5]
            if err:
                self.note(f"Model not changed: {clip(err.get('message') or err, 200)}")
            else:
                self.model_id = want
                self.transcript.write("model", model=want, name=self.model_name(want))
                self.say(f"\x1b[2mModel set to {self.model_name(want)}\x1b[0m")
                self.save_models()
            if not self.busy:
                self.idle()
        elif why == "prompt":
            self.flush()
            reason = res.get("stopReason") if not err else "error"
            if err:
                text = clip(err.get("message") or err, 500)
                self.transcript.write("error", text=f"Cursor: {text}")
                self.say(f"\x1b[31m{text}\x1b[0m")
            # a permission Cursor asked in this turn and no longer waits on: its card closes, never answered late
            for a in list(self.asks):
                self.answer(a, [], "Cursor stopped waiting on it", stale=True)
            self.transcript.write("turn_end", reason=reason)
            if reason == "cancelled":
                self.say("\x1b[2mStopped.\x1b[0m")
            self.busy = False
            self.idle()

    def ready(self, res, resumed):
        self.session = self.session or res.get("sessionId") or self.resume
        models = (res.get("models") or {}).get("availableModels") or []
        opts = next((o for o in res.get("configOptions") or [] if o.get("id") == "model"), None)
        if opts:
            models = [{"modelId": o.get("value"), "name": o.get("name")} for o in opts.get("options") or []] or models
        self.models = [{"id": m.get("modelId"), "name": m.get("name") or m.get("modelId")} for m in models if m.get("modelId")]
        self.model_id = (res.get("models") or {}).get("currentModelId") or (opts or {}).get("currentValue")
        self.transcript = Transcript(chats_dir(), self.session)
        lost = self.lost
        if lost:
            self.transcript.write("error", text=lost)
        self.transcript.write("session", session=self.session, resumed=resumed, model=self.model_id, name=self.model_name(self.model_id),
                              perm="full" if self.force else "manual", cwd=self.cwd)
        self.save_models()
        perm = "Full access" if self.force else "Ask"
        self.say(f"\x1b[1mCursor\x1b[0m · {self.model_name(self.model_id) or 'its default model'} · {perm} · chat {self.session}"
                 + (" (resumed)" if resumed else ""))
        if self.auto_asked and not self.force:
            self.transcript.write("error", text=AUTO_IGNORED)
            self.say(f"\x1b[33m{AUTO_IGNORED}\x1b[0m")
        if lost:
            self.say(f"\x1b[33m{lost}\x1b[0m")
        if self.first:
            self.queue.insert(0, (self.first, False))
        if self.model:
            self.set_model(self.model)   # its first turn waits for the model (idle() once it's set)
        else:
            self.idle()

    def model_value(self, want):
        """Cursor's model value for an id or name as typed: itself, or the one it names ("claude-opus-5-5" is
        "claude-opus-5-5[context=300k,...]")."""
        w = str(want or "").strip()
        for m in self.models:
            if m["id"] == w:
                return m["id"]
        for m in self.models:
            if m["id"].split("[", 1)[0] == w or (m["name"] or "").lower() == w.lower():
                return m["id"]
        return w

    def model_name(self, value):
        if not value:
            return None
        m = next((x for x in self.models if x["id"] == value), None)
        return (m or {}).get("name") or str(value).split("[", 1)[0]

    def set_model(self, want):
        value = self.model_value(want)
        if not value:
            return
        self.setting = True
        # what it saves as the person's default goes back once it's set (MODEL_KEYS)
        self.cfg = cursor_config()
        self.cfg_before = read_config(self.cfg)
        self.call("session/set_config_option", {"sessionId": self.session, "configId": "model", "value": value}, f"model:{value}")

    def save_models(self):
        """Its models for this account, for the model menus (core/agents.js DEFAULTS reads them)."""
        if not self.models:
            return
        p = os.path.join(chats_dir(), "models.json")
        try:
            with open(p + ".tmp", "w") as f:
                json.dump({"models": self.models, "current": self.model_id, "at": int(time.time())}, f)
            os.replace(p + ".tmp", p)
        except OSError:
            pass

    # ---- turns ----

    def idle(self):
        if self.setting:
            return
        if self.queue and not self.busy and self.session:
            text, logged = self.queue.pop(0)
            if logged:
                self.transcript.write("queue", op="dequeue", text=text)
            return self.submit(text)
        self.status("idle")
        self.box()

    def submit(self, text):
        text = text.strip("\n")
        if not text.strip():
            return
        # another agent's send comes wrapped (<hc from="t3">/model x</hc>): a command of this chat still counts
        bare = re.sub(r"^<hc from=[^>]*>([\s\S]*)</hc>$", r"\1", text.strip()).strip()
        if re.match(r"^/(model|exit|quit)\b", bare):
            text = bare
        if text.strip() in ("/exit", "/quit"):
            return self.stop()
        if self.busy or not self.session or self.setting:
            # in its turn after the one running (or once Cursor's session is up), as Claude Code queues it
            return self.enqueue(text)
        m = re.match(r"^/model(?:\s+(\S+))?\s*$", text.strip())
        if m:
            if not m.group(1):
                self.say("Models: " + ", ".join(x["id"].split("[", 1)[0] for x in self.models) if self.models else "No model list from Cursor.")
                return self.idle()
            return self.set_model(m.group(1))   # the queue goes on once it is set
        self.busy = True
        self.transcript.write("user", text=text)
        self.say(f"\x1b[1;36m›\x1b[0m {text}")
        self.status("working", "Thinking")
        self.call("session/prompt", {"sessionId": self.session, "prompt": [{"type": "text", "text": text}]}, "prompt")

    def enqueue(self, text):
        """A message for the turn after this one (shown in the chat's queue)."""
        logged = bool(self.transcript)
        self.queue.append((text, logged))
        if logged:
            self.transcript.write("queue", op="enqueue", text=text)
        self.say(f"\x1b[2m(queued) {clip(text.splitlines()[0], 120)}\x1b[0m")

    def flush(self):
        if self.text.strip() and self.transcript:
            self.transcript.write("agent", text=self.text.strip())
        if self.text and self.tty:
            self.emit("\r\n")
        self.text = ""

    def note(self, s):
        if self.transcript:
            self.transcript.write("error", text=s)
        self.say(f"\x1b[33m{s}\x1b[0m")

    def on_update(self, u):
        if self.loading:
            return   # the chat replayed as it loads: in the transcript already
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            t = (u.get("content") or {}).get("text") or ""
            if t:
                if not self.text and self.tty:
                    self.emit("\r\x1b[K")
                self.text += t
                self.emit(t.replace("\n", "\r\n") if self.tty else t)
        elif kind == "agent_thought_chunk":
            self.status("working", "Thinking")
        elif kind == "tool_call":
            self.flush()
            tid = call_id(u.get("toolCallId"))
            inp = u.get("rawInput") if isinstance(u.get("rawInput"), dict) else {}
            title = strip_ticks(u.get("title")) or u.get("kind") or "a tool"
            self.transcript.write("tool", call_id=tid, kind=u.get("kind") or "other", title=clip(title, 300), input=clipped(inp),
                                  path=tool_path(inp, u.get("locations")))
            self.say(f"\x1b[2m• {clip(title, 200)}\x1b[0m")
            self.status("working", clip(title, 200))
        elif kind == "tool_call_update" and u.get("status") in ("completed", "failed"):
            out = u.get("rawOutput")
            if out is None:
                out = "\n".join(((c.get("content") or {}).get("text") or "") for c in u.get("content") or [] if isinstance(c, dict))
            if isinstance(out, dict):
                o = out.get("stdout") or out.get("output") or out.get("content") or ""
                e = out.get("stderr") or ""
                out = (str(o) + ("\n" + str(e) if e else "")) if (o or e) else json.dumps(out)[:CLIP]
            self.transcript.write("tool_done", call_id=call_id(u.get("toolCallId")), status=u.get("status"), output=clip(out))
            if u.get("status") == "failed":
                self.say("\x1b[2m  (failed)\x1b[0m")
        elif kind == "plan":
            steps = [e.get("content") for e in u.get("entries") or [] if isinstance(e, dict)]
            if steps:
                self.flush()
                self.transcript.write("plan", steps=[clip(s, 300) for s in steps[:30]])
        elif kind == "current_mode_update":
            self.transcript.write("mode", mode=u.get("currentModeId"))

    # ---- permissions ----
    # Cursor runs tool calls side by side, so several session/request_permission can wait at once, each with its own
    # JSON-RPC id (seen in SHEP-17: a web fetch and a `gh` call asked together). Each is kept (self.asks, oldest first)
    # with its own card, and answered under its own id: none is overwritten or left unanswered. The terminal's menu is
    # the oldest one's; every card answers its own.

    def on_request(self, m):
        method = m.get("method")
        if method != "session/request_permission":
            self.send({"jsonrpc": "2.0", "id": m.get("id"), "error": {"code": -32601, "message": f"{method} isn't offered by this client"}})
            return
        p = m.get("params") or {}
        tool = p.get("toolCall") or {}
        self.flush()
        req = hook_request(tool, self.cwd, self.session)
        title = strip_ticks(tool.get("title")) or req["tool_name"]
        why = req["tool_input"].get("description") or req["tool_input"].get("why") or ""
        # the same id again (never seen): the earlier one is no longer what Cursor waits on
        for old in [a for a in self.asks if a["rpc"] == m.get("id")]:
            self.answer(old, [], "Cursor asked it again", stale=True)
        a = {"rpc": m.get("id"), "options": p.get("options") or [], "req": req, "title": title, "why": why,
             "kind": tool.get("kind") or "other", "at": time.time(), "id": None}
        self.asks.append(a)
        if self.transcript:
            self.transcript.write("permission", call_id=call_id(tool.get("toolCallId")), title=clip(title, 300), detail=clip(why, 300))
        self.leave_ask(a)
        if a is self.asks[0]:
            self.show_ask()
        else:
            self.say(f"\x1b[1;33mCursor also asks:\x1b[0m {clip(title, 300)}"
                     f"\x1b[2m (its menu comes here once the one above is answered; its card in the chat answers it now)\x1b[0m")
        self.status("blocked", report_msg(self.asks[0]["req"]), "permission")

    def show_ask(self):
        """The oldest waiting permission's menu, which the terminal's keys answer."""
        a = self.asks[0]
        n = len(self.asks)
        nl = "\r\n" if self.tty else "\n"
        lines = [f"\x1b[1;33mCursor asks{f' (1 of {n})' if n > 1 else ''}:\x1b[0m {clip(a['title'], 300)}"] + ([f"  {a['why']}"] if a["why"] else [])
        for i, o in enumerate(a["options"], 1):
            label = o.get("name") or o.get("optionId")
            scope = always_scope(a) if o.get("kind") == "allow_always" else None
            lines.append(f"  {i}. {label}" + (f" \x1b[2m({scope})\x1b[0m" if scope else ""))
        keys = ", ".join(str(i) for i in range(1, len(a["options"]) + 1))
        lines.append(f"\x1b[2m  Press {keys or 'Esc'} (Esc or Ctrl+C rejects). A message typed or sent now waits its turn: it never answers this. The chat's card answers it too.\x1b[0m")
        self.emit("\r\x1b[K" + nl.join(lines) + nl)
        self.box()

    def leave_ask(self, a):
        """The request where the chat's cards find it (ask-hook.sh's files), only in a Herdr terminal. Its id is
        <terminal>.<seconds>.<pid>, as the hook's; two asked in the same second get the next free second, so each
        card has its own id (never the same file for two requests)."""
        t = self.env.get("HC_TERMINAL_ID") or ""
        if not t or "/" in t or ".." in t:
            return
        d = asks_dir()
        sec = max(int(time.time()), self.ask_sec + 1)
        self.ask_sec = sec
        aid = f"{t}.{sec}.{os.getpid()}"
        try:
            for sub in ("req", "ans", "end"):
                os.makedirs(os.path.join(d, sub), exist_ok=True)
            for old in (os.path.join(d, "ans", aid + ".json"), os.path.join(d, "end", aid)):
                if os.path.exists(old):
                    os.remove(old)
            p = os.path.join(d, "req", aid + ".json")
            with open(p + ".tmp", "w") as f:
                json.dump(a["req"], f)
            os.replace(p + ".tmp", p)
            a["id"] = aid
        except OSError:
            pass

    def check_asks(self):
        """The chat's answers to the permissions waiting now, each by its own card."""
        for a in list(self.asks):
            if a not in self.asks:
                continue
            if a["id"]:
                p = os.path.join(asks_dir(), "ans", a["id"] + ".json")
                try:
                    with open(p) as f:
                        text = f.read().strip()
                except OSError:
                    text = None
                if text is not None:
                    try:
                        d = json.loads(text)["hookSpecificOutput"]["decision"] if text else None
                    except (ValueError, KeyError, TypeError):
                        d = None
                    if not isinstance(d, dict) or d.get("behavior") not in ("allow", "deny"):
                        # no decision ("Answer in terminal", or one it can't read): the terminal answers it
                        self.forget(a)
                        continue
                    if d["behavior"] == "allow":
                        # the card's Allow is Cursor's Allow once, never always (always changes Cursor's allowlist)
                        self.answer(a, ["allow_once"], None)
                    else:
                        msg = str(d.get("message") or "")
                        words = msg[len(WROTE_INSTEAD):].strip() if msg.startswith(WROTE_INSTEAD) else None
                        self.answer(a, ["reject_once", "reject_always"], None, words=words or None)
                    continue
            if time.time() - a["at"] > ASK_WAIT:
                self.answer(a, ["reject_once", "reject_always"], "No answer in 30 minutes: rejected")

    def put_back(self):
        """The person's Cursor default model as it was before this chat changed its own (MODEL_KEYS)."""
        due = [t for t in self.restores if t <= time.time()]
        if not due:
            return
        self.restores = [t for t in self.restores if t > time.time()]
        restore_model_keys(self.cfg, self.cfg_before)

    def forget(self, a):
        """Its card's files gone (answered, or left to the terminal)."""
        if a.get("id"):
            for sub in ("req", "ans"):
                try:
                    os.remove(os.path.join(asks_dir(), sub, a["id"] + ".json"))
                except OSError:
                    pass
            a["id"] = None

    def answer(self, a, kinds, end, words=None, stale=False):
        """Answers one waiting permission, under its own request id, with Cursor's option of the first of kinds
        (none: cancelled). end: how it went, for its card when it wasn't answered there (None: the card answered it
        and says so itself). words: what the person wrote in the chat instead of using the card, which Cursor's reject
        can't carry: it goes as their next message. stale: Cursor no longer waits on it."""
        if a not in self.asks:
            return
        front = a is self.asks[0]
        o = None if stale else option_for(a["options"], kinds)
        outcome = {"outcome": "selected", "optionId": o["optionId"]} if o else {"outcome": "cancelled"}
        self.send({"jsonrpc": "2.0", "id": a["rpc"], "result": {"outcome": outcome}})
        allowed = bool(o) and str(o.get("kind", "")).startswith("allow")
        always = bool(o) and o.get("kind") == "allow_always"
        if a.get("id") and end:
            try:
                p = os.path.join(asks_dir(), "end", a["id"])
                with open(p + ".tmp", "w") as f:
                    f.write(end + "\n")
                os.replace(p + ".tmp", p)
            except OSError:
                pass
        self.forget(a)
        self.asks.remove(a)
        by = "terminal" if end else "chat"
        result = "stale" if stale else ("allowed" if allowed else "declined")
        if self.transcript:
            self.transcript.write("permission_done", outcome=result, by=by, option=(o or {}).get("optionId"),
                                  kind=(o or {}).get("kind"), title=clip(a["title"], 300))
        scope = always_scope(a) if always else None
        word = "No longer asked" if stale else ("Allowed always" if always else "Allowed once") if allowed else "Rejected"
        self.say(f"\x1b[2m{word}{' in the chat' if by == 'chat' else ''}{f' ({scope})' if scope else ''}: {clip(a['title'], 100)}\x1b[0m")
        if words:
            self.enqueue(words)
        if self.asks:
            if front:
                self.show_ask()
            self.status("blocked", report_msg(self.asks[0]["req"]), "permission")
        elif self.busy:
            self.status("working", "Working")
        else:
            self.idle()

    # ---- typing ----

    def on_key(self, data):
        if self.asks and not self.pasting and not self.buffer:
            # A key of its own answers the menu: one digit, Esc or Ctrl+C, each read alone (a person's keypress). A
            # paste, a sequence (an arrow, a mouse or focus report) or words are never an answer.
            a = self.asks[0]
            if len(data) == 1 and data.isdigit():
                k = int(data)
                if 1 <= k <= len(a["options"]):
                    o = a["options"][k - 1]
                    return self.answer(a, [o.get("kind")], how_in_terminal(o))
                return
            if data in ("\x1b", "\x03"):
                return self.answer(a, ["reject_once", "reject_always"], "Rejected in the terminal")
            if data in ("\r", "\n", "\r\n"):
                # Enter alone picks nothing: an approval is a number pressed on purpose
                self.say(f"\x1b[2mPress {', '.join(str(i) for i in range(1, len(a['options']) + 1))} to answer (Esc rejects).\x1b[0m")
                return
        i = 0
        while i < len(data):
            if data.startswith("\x1b[200~", i):
                self.pasting = True
                i += 6
                continue
            if data.startswith("\x1b[201~", i):
                self.pasting = False
                i += 6
                continue
            ch = data[i]
            i += 1
            if self.pasting:
                self.buffer += "\n" if ch == "\r" else ch
                continue
            if ch in ("\r", "\n"):
                text, self.buffer = self.buffer, ""
                self.emit("\r\x1b[K")
                self.submit(text)
            elif ch in ("\x7f", "\x08"):
                self.buffer = self.buffer[:-1]
            elif ch == "\x03":
                if self.asks:
                    self.buffer = ""
                elif self.busy:
                    self.cancel()
                elif self.buffer:
                    self.buffer = ""
                else:
                    self.ctrl_c += 1
                    if self.ctrl_c >= 2:
                        return self.stop()
                    self.say("\x1b[2mPress Ctrl+C again to exit.\x1b[0m")
                    continue
            elif ch == "\x04" and not self.buffer and not self.busy:
                return self.stop()
            elif ch == "\x1b":
                # Esc alone stops a turn; an arrow's, a mouse or focus report's or another key's sequence is skipped
                if i < len(data) and data[i] in "[O":
                    csi = data[i] == "["
                    i += 1
                    if csi and i < len(data) and data[i] == "M":
                        i += 4   # an X10 mouse report: ESC [ M and three bytes
                        continue
                    while i < len(data) and not data[i].isalpha() and data[i] != "~":
                        i += 1
                    i += 1
                elif i < len(data) and data[i] != "\x1b":
                    i += 1   # Alt and a key
                elif self.busy and not self.asks:
                    self.cancel()
            elif ch == "\x15":
                self.buffer = ""
            elif ch >= " " or ch == "\t":
                self.buffer += ch
            self.ctrl_c = 0 if ch != "\x03" else self.ctrl_c
        self.box()

    def cancel(self):
        if self.busy and self.session:
            self.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session}})

    # ---- the end ----

    def fail(self, why):
        self.code = 1
        if self.transcript:
            self.transcript.write("error", text=why)
        self.say(f"\x1b[31m{why}\x1b[0m")
        self.status("error", why)
        self.done = True

    def stop(self):
        self.done = True

    def close(self):
        if self.restores:
            self.restores = [0]
            self.put_back()
        for a in list(self.asks):
            self.answer(a, ["reject_once", "reject_always"], "Agent stopped")
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        if self.transcript:
            self.transcript.close()
        self.emit("\x1b]7501;state=clear\x07")
        if self.tty:
            self.emit("\x1b[?2004l\r\n")

    # ---- the loop ----

    def read_input(self):
        fd = self.inp.fileno()
        while not self.done:
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
                if not r:
                    continue
                data = os.read(fd, 65536)
            except (OSError, ValueError):
                break
            if not data:
                self.events.put(("eof", None))
                break
            self.events.put(("key", data.decode("utf-8", "replace")))

    def handle(self, ev, val):
        if ev == "acp":
            if "id" in val and ("result" in val or "error" in val) and "method" not in val:
                self.on_response(val)
            elif val.get("method") == "session/update":
                self.on_update((val.get("params") or {}).get("update") or {})
            elif "id" in val and "method" in val:
                self.on_request(val)
        elif ev == "key":
            self.on_key(val)
        elif ev == "eof":
            # no more typing (a test's or a pipe's input): it ends once what was asked is answered
            self.eof = True
        elif ev == "exit":
            if not self.done:
                self.flush()
                tail = self.err_tail[-1] if self.err_tail else ""
                self.fail(f"Cursor Agent stopped (exit {val})" + (f": {tail}" if tail else "") + ".")

    def run(self):
        old = None
        if self.tty:
            import termios
            import tty
            old = termios.tcgetattr(self.inp.fileno())
            tty.setraw(self.inp.fileno())
            self.emit("\x1b[?2004h")
        try:
            self.say("\x1b[2mStarting Cursor Agent…\x1b[0m")
            self.status("working", "Starting Cursor")
            if not self.start_agent():
                return self.code
            threading.Thread(target=self.read_input, daemon=True).start()
            while not self.done:
                try:
                    ev, val = self.events.get(timeout=0.3)
                except queue.Empty:
                    ev, val = None, None
                if ev:
                    self.handle(ev, val)
                self.check_asks()
                self.put_back()
                if self.eof and not self.busy and not self.queue and not self.asks and self.session:
                    self.done = True
            return self.code
        finally:
            self.close()
            if old is not None:
                import termios
                termios.tcsetattr(self.inp.fileno(), termios.TCSADRAIN, old)


def run(argv):
    def end(*_):
        raise SystemExit(1)
    # hc kill or a closed terminal: Cursor's process ends with it (close()), never left running
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, end)
    return Bridge(argv).run()

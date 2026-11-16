"""SHEP-1: a picture the person shared in a chat is read without asking, and nothing else is.

JHQ-6: the person pasted a CleanShot screenshot (a copied file: its own path, with spaces) in the Assistant's chat, the
Assistant put the path in the task's brief, and the Claude worker's Read of it, outside its worktree, waited on the
chat's permission card. The chat now lists the pictures it delivers (mods/chat/attach.js SHARED, data/chat-shared.tsv)
and herdr-task's shared.py allows Claude's Read of those, of the chat's own pasted and uploaded ones, and of the copies
`hc put` lands from another machine; the worker's hook (`herdr-task _allow`) and the chat's (ask-hook.sh) ask it first.

The window's shell steps run as attach.js writes them (node evaluates the strings); herdr-task's hook code runs in a
python -c on a throwaway home (home_guard), never the herdr-task entry with a changed HOME."""
import home_guard  # noqa: F401  (first: every test on a throwaway home, never the real one)
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
ATTACH = os.path.join(ROOT, "mods", "chat", "attach.js")
HOOK = os.path.join(ROOT, "mods", "chat", "ask-hook.sh")
SRC = open(ATTACH, encoding="utf-8").read()
ALLOW = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
# JHQ-6's own picture, as CleanShot names it (spaces, an @2x), under this test's home
CLEANSHOT = os.path.join("Library", "Application Support", "CleanShot", "media", "media_M4r5RtyMxv", "CleanShot 2026-11-16 at 11.30.13@2x.png")


def js_const(name):
    """attach.js's `export const <name> = <expr>;` evaluated by node, as the window's JS does."""
    m = re.search(r"export const %s = (.*?);\n" % name, SRC, re.S)
    return subprocess.run(["node", "-e", "process.stdout.write(String(eval(require('fs').readFileSync(0, 'utf8'))))"],
                          input=m.group(1), capture_output=True, text=True, check=True).stdout


def js_shareable(files):
    """attach.js's shareable() on these files (isImage as it is there)."""
    img = re.search(r"const IMAGE = (/.*?/i);", SRC).group(1)
    fn = re.search(r"export const shareable = (.*?);\n", SRC).group(1)
    listable = re.search(r"const listable = (.*?);\n", SRC).group(1)
    code = (f"const IMAGE = {img}; const isImage = (p) => IMAGE.test(p); const listable = {listable}; const shareable = {fn};"
            "process.stdout.write(JSON.stringify(shareable(JSON.parse(require('fs').readFileSync(0, 'utf8')))))")
    return json.loads(subprocess.run(["node", "-e", code], input=json.dumps(files), capture_output=True, text=True, check=True).stdout)


class Fixture:
    """A throwaway home with JHQ-6's screenshot in it, the window's chat and the hooks as they run."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="shared-"))
        self.home = os.path.join(self.tmp, "home")
        self.app = os.path.join(self.home, ".config", "herdr-app")
        os.makedirs(os.path.join(self.app, "data", "chat-attach", "view"))
        self.shot = self.picture(os.path.join(self.home, CLEANSHOT))
        self.ledger = os.path.join(self.app, "data", "chat-shared.tsv")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def picture(self, path, data=PNG):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def env(self, **more):
        e = {k: v for k, v in os.environ.items() if k not in ("HERDR_ASK_CHAINED", "XDG_STATE_HOME")}
        return {**e, "HOME": self.home, "HERDR_APP_HOME": self.app, "SHEPHERDR_NO_MIGRATE": "1", **more}

    def share(self, *paths, at=None):
        """The window's chat delivering these pictures to an agent here (attach.js deliver -> SHARED)."""
        at = str(int((time.time() if at is None else at) * 1000))
        subprocess.run(["sh", "-c", js_const("SHARED"), "share", self.ledger, at, *paths], check=True, env=self.env())

    def worker_hook(self, req):
        """A task worker's PermissionRequest hook as Claude Code runs it (`herdr-task _allow`, launch.cmd_allow): its
        stdout. No chat card here (no ask-hook.sh in this app home), so anything it doesn't allow gets no decision."""
        code = ("import sys, argparse; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]);"
                "from herdr_task.launch import cmd_allow; cmd_allow(argparse.Namespace(command='/x/herdr-task'))")
        r = subprocess.run([sys.executable, "-c", code, BIN], input=json.dumps(req), capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def read(self, path):
        return self.worker_hook({"hook_event_name": "PermissionRequest", "tool_name": "Read", "tool_input": {"file_path": path}})

    def allowed(self, path):
        return self.read(path) == json.dumps(ALLOW)



@unittest.skipUnless(shutil.which("node"), "the window's steps are evaluated by node")
class SharedPictures(Fixture, unittest.TestCase):
    # ---- the reported case ----

    def test_jhq6_brief_screenshot_reads_without_asking(self):
        # before the person shares it: the worker's Read of a picture outside its folder still asks (no decision here)
        self.assertEqual(self.read(self.shot), "")
        # pasted in the chat and sent: its path, spaces and all, in the list; the brief's Read of it no longer asks
        self.share(self.shot)
        with open(self.ledger) as f:
            at, path = f.read().rstrip("\n").split("\t")
        self.assertEqual(path, self.shot)
        self.assertTrue(at.isdigit())
        self.assertTrue(self.allowed(self.shot))
        # the same file by another spelling of its path (a symlinked folder) is the same picture
        os.symlink(os.path.dirname(self.shot), os.path.join(self.tmp, "link"))
        self.assertTrue(self.allowed(os.path.join(self.tmp, "link", os.path.basename(self.shot))))

    def test_the_chats_own_pasted_and_uploaded_pictures(self):
        attach = os.path.join(self.app, "data", "chat-attach")
        self.assertTrue(self.allowed(self.picture(os.path.join(attach, "pasted-20261116T113013.png"))))
        self.assertTrue(self.allowed(self.picture(os.path.join(attach, "web-mabc12-IMG 0042.jpg"), b"\xff\xd8\xff\xe0" + b"\0" * 12)))
        # view/ links any picture a chat shows (an agent's too), and other names aren't the chat's own
        self.assertFalse(self.allowed(self.picture(os.path.join(attach, "view", "123-pasted-x.png"))))
        self.assertFalse(self.allowed(self.picture(os.path.join(attach, "shot.png"))))
        # a pasted text file is the chat's, but not a picture
        self.assertFalse(self.allowed(self.picture(os.path.join(attach, "pasted-abc-notes.txt"), b"hello")))

    def test_a_copy_hc_put_landed_from_another_machine(self):
        state = os.path.join(self.home, ".local", "state", "herdr-core")
        self.assertTrue(self.allowed(self.picture(os.path.join(state, "default", "files", "t3", "CleanShot_2026-11-16_at_11.30.13_2x.png"))))
        self.assertFalse(self.allowed(self.picture(os.path.join(state, "default", "files", "t3", "deeper", "x.png"))))
        self.assertFalse(self.allowed(self.picture(os.path.join(state, "default", "other", "t3", "x.png"))))

    # ---- boundaries: nothing else is allowed ----

    def test_an_unshared_picture_still_asks(self):
        other = self.picture(os.path.join(self.home, "Desktop", "private.png"))
        self.share(self.shot)
        self.assertFalse(self.allowed(other))

    def test_only_pictures_even_when_listed(self):
        secret = self.picture(os.path.join(self.home, ".ssh", "id_ed25519"), b"-----BEGIN OPENSSH PRIVATE KEY-----")
        fake = self.picture(os.path.join(self.home, "Desktop", "keys.png"), b"-----BEGIN OPENSSH PRIVATE KEY-----")
        self.share(secret, fake)
        self.assertFalse(self.allowed(secret))
        self.assertFalse(self.allowed(fake), "a picture's name with no picture's bytes")

    def test_a_shared_path_swapped_for_a_link_to_a_secret(self):
        self.share(self.shot)
        secret = self.picture(os.path.join(self.home, ".ssh", "id_ed25519"), b"-----BEGIN OPENSSH PRIVATE KEY-----")
        os.unlink(self.shot)
        os.symlink(secret, self.shot)
        self.assertFalse(self.allowed(self.shot))

    def test_old_relative_missing_and_not_read(self):
        self.share(self.shot, at=time.time() - 31 * 86400)
        self.assertFalse(self.allowed(self.shot), "shared more than 30 days ago")
        self.share(self.shot)
        self.assertFalse(self.allowed(os.path.relpath(self.shot)))
        self.assertFalse(self.allowed(self.shot + ".missing.png"))
        # another tool on the same file (a shell's cat) is still the person's call
        bash = {"tool_name": "Bash", "tool_input": {"command": f"cat '{self.shot}'"}}
        self.assertEqual(self.worker_hook(bash), "")
        edit = {"tool_name": "Write", "tool_input": {"file_path": self.shot, "content": "x"}}
        self.assertEqual(self.worker_hook(edit), "")

    def test_a_huge_file_is_no_screenshot(self):
        code = ("import sys; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]);"
                "from herdr_task import shared; shared.MAX_BYTES = 8; print(shared.shared_picture(sys.argv[2]))")
        self.share(self.shot)
        r = subprocess.run([sys.executable, "-c", code, BIN, self.shot], capture_output=True, text=True, env=self.env())
        self.assertEqual(r.stdout.strip(), "False", r.stderr)

    def test_what_the_window_lists(self):
        files = [{"path": self.shot, "image": True},
                 {"path": "/a/notes.txt", "image": False},
                 {"path": "/a/tab\there.png", "image": True},
                 {"path": "relative.png", "image": True},
                 # a script's send through data/chat-sendreq.json: not the person's own share
                 {"path": "/a/relayed.png", "image": True, "relayed": True}]
        self.assertEqual(js_shareable(files), [self.shot])

    def test_prune_keeps_thirty_days(self):
        self.share(self.picture(os.path.join(self.home, "old.png")), at=time.time() - 40 * 86400)
        self.share(self.shot)
        cut = str(int((time.time() - 30 * 86400) * 1000))
        subprocess.run(["sh", "-c", js_const("PRUNE"), "x", os.path.join(self.app, "data", "chat-attach"), self.ledger, cut],
                       check=True, env=self.env())
        with open(self.ledger) as f:
            self.assertEqual([l.split("\t")[1] for l in f.read().splitlines()], [self.shot])

    def test_the_launch_grants_no_files(self):
        """No blanket rule: a worker's --settings still allows only its own commands (the check is per request)."""
        sys.path.insert(0, BIN)
        from herdr_task import launch
        argv = launch.own_argv(["claude", "go"])
        st = json.loads(argv[argv.index("--settings") + 1])
        self.assertFalse([r for r in st.get("permissions", {}).get("allow", []) if not r.startswith("Bash(")])
        self.assertNotIn("additionalDirectories", st.get("permissions", {}))


# The chat's own hook for any agent (an Assistant, a plain Claude Code): hc get answers a live working agent with no
# worker hook, so it would put up a card.
FAKE_HC = """#!/bin/sh
[ "$1" = get ] || exit 1
echo '{"state": "live", "status": "working", "argv": ["claude"]}'
"""
# herdr-task on PATH as the hook finds it: only its _shared-read, run from this checkout
FAKE_TASK = """#!/bin/sh
[ "$1" = _shared-read ] || exit 1
exec "%s" -c 'import sys; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]); from herdr_task.shared import cmd_shared_read; cmd_shared_read(None)' "%s"
"""


# The chat's needShared (mods/chat/main.js) on rows the chat built from a real-shaped agent log: core/chat-log.js parses
# it, core/chat-view.js briefLabel sets apart what the person didn't type (as main.js rows() does), and attach.js's
# sentPictures/shareAt write the list. argv: repo, log file, its CLI, the ledger, the chat's machine ("" = here).
NEED_SHARED = r"""
const [repo, log, kind, ledger, machine] = process.argv.slice(1);
const fs = require("fs"), { execFileSync } = require("child_process");
(async () => {
  const { Conversation, records } = await import(`${repo}/core/chat-log.js`);
  const { briefLabel } = await import(`${repo}/core/chat-view.js`);
  const src = fs.readFileSync(`${repo}/mods/chat/attach.js`, "utf8");
  const grab = (re) => re.exec(src)[0].replace(/^export /, "");
  const sentPictures = new Function([grab(/const IMAGE = .*;/), grab(/export const isImage = .*;/), grab(/const listable = .*;/),
    grab(/export const sentPictures = [\s\S]*?filter\(listable\);/), "return sentPictures;"].join("\n"))();
  const SHARED = eval(/export const SHARED = ('[^\n]*'\n\s*\+ '[^\n]*');/.exec(src)[1]);
  const attach = { sentPictures, shareAt: async (list, at) => { execFileSync("sh", ["-c", SHARED, "share", ledger, String(Math.round(at)), ...list]); } };
  const main = fs.readFileSync(`${repo}/mods/chat/main.js`, "utf8");
  const body = /\n  needShared\(rows, cx\) \{\n([\s\S]*?)\n  \}\n/.exec(main)[1];
  const needShared = new Function("attach", `return function (rows, cx) {\n${body}\n}`)(attach);
  let off = 0;
  const out = fs.readFileSync(log, "utf8").split("\n").filter(Boolean).map((l) => { const r = `${off}\tJ\t${l}`; off += l.length + 1; return r; }).join("\n");
  const c = new Conversation(kind);
  c.feed(records(`head\n${out}`).records);
  const first = c.items.findIndex((x) => x.kind === "user");
  const rows = c.items.map((x, i) => (x.kind === "user" && briefLabel(x, i, { first, start: 0, template: null, task: false }) ? { kind: "brief", id: x.id, text: x.text } : x));
  const later = [];
  const cx = { timer: { after: (_, f) => later.push(f) }, spawn: (f) => later.push(f) };
  const self = { machine: machine || null, sharedAsked: new Set() };
  for (let pass = 0; pass < 2; pass++) {
    needShared.call(self, rows, cx);
    while (later.length) await later.shift()(cx);
  }
})().catch((e) => { console.error(e); process.exit(1); });
"""


@unittest.skipUnless(shutil.which("node"), "the window's steps are evaluated by node")
class PicturesSentBeforeTheList(Fixture, unittest.TestCase):
    """JHQ-9: the person pasted a CleanShot picture in the Assistant's (Codex) chat before the list was kept; the task's
    worker read it from its brief and waited on the card. The chat lists the pictures in the person's own messages of
    the log it shows, with each message's time, so a picture sent before the update is read without asking too."""

    def codex_log(self, *messages):
        """A Codex rollout as it writes one: each (role, text, seconds ago) a message."""
        path = os.path.join(self.tmp, "rollout.jsonl")
        with open(path, "w") as f:
            for role, text, ago in messages:
                at = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ago))
                kind = "input_text" if role == "user" else "output_text"
                f.write(json.dumps({"timestamp": at, "type": "response_item", "payload": {"type": "message", "role": role,
                                    "content": [{"type": kind, "text": text}]}}) + "\n")
        return path

    def chat_shows(self, log, kind="codex", machine=""):
        r = subprocess.run(["node", "-e", NEED_SHARED, ROOT, log, kind, self.ledger, machine], capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)

    def lines(self):
        return open(self.ledger).read().splitlines() if os.path.exists(self.ledger) else []

    def test_jhq9_a_picture_sent_before_the_update(self):
        relayed = self.picture(os.path.join(self.home, "Desktop", "from an agent.png"))
        typed = self.picture(os.path.join(self.home, "Desktop", "inline.png"))
        log = self.codex_log(
            ("user", f"outra coisa: nested spaces, igual os assistants\n\n{self.shot}", 600),
            ("assistant", f"Looked at it.\n{relayed}", 590),
            ("user", f'<hc from="t22" title="JHQ-9">here is mine\n{relayed}</hc>', 580),
            ("user", f"look at {typed} please", 570))
        self.assertFalse(self.allowed(self.shot), "before the chat shows it: the brief's picture asks (as JHQ-9's did)")
        self.chat_shows(log)
        self.assertTrue(self.allowed(self.shot), "the person's own picture, sent before the list was kept")
        self.assertEqual(len(self.lines()), 1, "drawn twice, written once")
        at = int(self.lines()[0].split("\t")[0]) / 1000
        self.assertAlmostEqual(at, time.time() - 600, delta=5, msg="listed with its message's time, not now")
        self.assertFalse(self.allowed(relayed), "another agent's message, or the agent's own words, aren't the person's")
        self.assertFalse(self.allowed(typed), "a path inside a sentence isn't a picture sent")

    def test_an_old_message_keeps_its_age(self):
        log = self.codex_log(("user", f"see\n{self.shot}", 31 * 86400))
        self.chat_shows(log)
        self.assertEqual(len(self.lines()), 1)
        self.assertFalse(self.allowed(self.shot), "sent 31 days ago: past the list's 30 days, so it asks")

    def test_claude_codes_image_sources_too(self):
        path = os.path.join(self.tmp, "claude.jsonl")
        at = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "timestamp": at, "message": {"role": "user", "content": [
                {"type": "text", "text": "[Image #1] this one"}, {"type": "image", "source": {"type": "base64", "data": ""}}]}}) + "\n")
            f.write(json.dumps({"type": "user", "isMeta": True, "timestamp": at, "message": {"role": "user", "content": [
                {"type": "text", "text": f"[Image: source: {self.shot}]"}]}}) + "\n")
        self.chat_shows(path, kind="claude")
        self.assertTrue(self.allowed(self.shot))

    def test_another_machines_chat_lists_nothing_here(self):
        self.chat_shows(self.codex_log(("user", f"see\n{self.shot}", 60)), machine="mini")
        self.assertEqual(self.lines(), [])
        self.assertFalse(self.allowed(self.shot))


@unittest.skipUnless(shutil.which("jq") and shutil.which("node"), "the hook watches the terminal with jq")
class ChatHookAllowsSharedPictures(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name, text in (("hc", FAKE_HC), ("herdr-task", FAKE_TASK % (sys.executable, BIN))):
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(text)
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.asks = os.path.join(self.app, "data", "chat-asks", "req")

    def hook(self, req):
        e = self.env(PATH=f"{self.bin}:{os.environ['PATH']}", HC_TERMINAL_ID="t9")
        p = subprocess.Popen(["sh", HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=e)
        p.stdin.write(json.dumps(req).encode())
        p.stdin.close()
        return p

    def test_an_assistant_reads_a_shared_picture_without_a_card(self):
        self.share(self.shot)
        p = self.hook({"tool_name": "Read", "tool_input": {"file_path": self.shot}})
        p.wait(timeout=10)
        with p.stdout:
            self.assertEqual(json.loads(p.stdout.read()), ALLOW)
        self.assertEqual(os.listdir(self.asks) if os.path.isdir(self.asks) else [], [], "no card was left")

    def test_anything_else_still_gets_the_card(self):
        self.share(self.shot)
        for req in ({"tool_name": "Read", "tool_input": {"file_path": self.picture(os.path.join(self.home, "Desktop", "x.png"))}},
                    {"tool_name": "Bash", "tool_input": {"command": f"cat '{self.shot}'"}}):
            p = self.hook(req)
            end = time.time() + 5
            while time.time() < end and not (os.path.isdir(self.asks) and os.listdir(self.asks)):
                time.sleep(0.05)
            self.assertTrue(os.listdir(self.asks), f"a card for {req['tool_name']}")
            p.kill()
            p.wait()
            p.stdout.close()
            for n in os.listdir(self.asks):
                os.unlink(os.path.join(self.asks, n))

    def test_a_worker_hooks_chained_copy_does_not_ask_again(self):
        self.share(self.shot)
        e = self.env(PATH=f"{self.bin}:{os.environ['PATH']}", HC_TERMINAL_ID="t9", HERDR_ASK_CHAINED="1")
        p = subprocess.Popen(["sh", HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=e)
        p.stdin.write(json.dumps({"tool_name": "Read", "tool_input": {"file_path": self.shot}}).encode())
        p.stdin.close()
        end = time.time() + 5
        while time.time() < end and not (os.path.isdir(self.asks) and os.listdir(self.asks)):
            time.sleep(0.05)
        self.assertTrue(os.listdir(self.asks), "chained: _allow already decided, so this copy only files the card")
        p.kill()
        p.wait()
        p.stdout.close()


if __name__ == "__main__":
    unittest.main()

"""herdr-task cursor-chat (SHEP-6): Cursor Agent as an Assistant over its ACP server, against a stand-in server
(fixtures/fake_cursor_acp.py, the messages the real one sent). What it shows, the transcript the chat reads, its
status reports, permissions through the chat's card files, resume, and its honest failures."""
import home_guard  # noqa: F401  (first: every test on a throwaway home, never the real one)
import base64
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "mods", "tasks", "bin", "herdr-task")
FAKE = os.path.join(ROOT, "test", "fixtures", "fake_cursor_acp.py")
SID = "11111111-2222-3333-4444-555555555555"


def statuses(out):
    """Its OSC 7501 reports: [(state, msg, kind)]."""
    found = []
    for body in re.findall(r"\x1b\]7501;([^\x07]*)\x07", out):
        kv = dict(p.split("=", 1) for p in body.split(":") if "=" in p)
        msg = base64.b64decode(kv["msg"]).decode() if "msg" in kv else None
        found.append((kv.get("state"), msg, kv.get("kind"), kv.get("app")))
    return found


class CursorChat(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fake = os.path.join(self.dir, "cursor-agent")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        os.chmod(fake, 0o755)
        self.chats = os.path.join(self.dir, "chats")
        self.app = os.path.join(self.dir, "app")
        self.acp = os.path.join(self.dir, "acp.log")
        self.env = {**os.environ, "HERDR_CURSOR_BIN": fake, "HERDR_CURSOR_CHATS": self.chats, "FAKE_ACP_LOG": self.acp,
                    "HERDR_APP_HOME": self.app, "SHEPHERDR_NO_MIGRATE": "1"}
        self.env.pop("HC_TERMINAL_ID", None)

    def start(self, *args, env=None):
        return subprocess.Popen([sys.executable, ENTRY, "cursor-chat", *args], cwd=self.dir, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**self.env, **(env or {})})

    def run_bridge(self, typed, *args, env=None):
        p = self.start(*args, env=env)
        out, err = p.communicate(typed, timeout=30)
        return p.returncode, out, err

    def transcript(self, sid=SID):
        with open(os.path.join(self.chats, f"{sid}.jsonl")) as f:
            return [json.loads(l) for l in f]

    def sent(self):
        with open(self.acp) as f:
            return [json.loads(l) for l in f]

    def wait_for(self, pattern, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            hits = glob.glob(pattern)
            if hits:
                return hits
            time.sleep(0.05)
        self.fail(f"nothing at {pattern}")

    def test_talks_and_keeps_a_transcript(self):
        code, out, err = self.run_bridge("say hello there\n/model composer-2.5\nsay again\n", "--model", "claude-opus-5-5")
        self.assertEqual(code, 0, err)
        recs = self.transcript()
        self.assertTrue(all(r["herdr"] == "cursor" for r in recs))
        self.assertEqual([r["type"] for r in recs], ["session", "model", "user", "agent", "turn_end", "model", "user", "agent", "turn_end"])
        self.assertEqual(recs[0]["perm"], "manual")
        self.assertEqual(recs[1]["model"], "claude-opus-5-5[context=300k,effort=medium,fast=false]")   # the name typed, Cursor's value
        self.assertEqual(recs[3]["text"], "hello there")
        self.assertEqual(recs[5]["model"], "composer-2.5[fast=true]")
        self.assertIn("hello there", out)
        st = statuses(out)
        self.assertTrue(all(s[3] == "cursor" for s in st if s[0] != "clear"))
        self.assertIn(("working", "Thinking", None, "cursor"), st)
        self.assertIn(("idle", None, None, "cursor"), st)
        # the models Cursor lists for this account, for the menus
        with open(os.path.join(self.chats, "models.json")) as f:
            self.assertIn("composer-2.5[fast=true]", [m["id"] for m in json.load(f)["models"]])
        # one Cursor process, started as `acp` with no --force: Ask is Cursor's own allowlist
        argvs = [r["argv"] for r in self.sent() if "argv" in r]
        self.assertEqual(argvs, [["acp"]])

    def test_a_model_for_this_chat_leaves_the_persons_default(self):
        """Cursor saves a session's model as the default (cli-config.json): put back, everything else kept."""
        cfg_dir = os.path.join(self.dir, "cursor-home")
        os.makedirs(cfg_dir)
        cfg = os.path.join(cfg_dir, "cli-config.json")
        before = {"permissions": {"allow": ["Shell(ls)"], "deny": []}, "model": {"modelId": "default", "displayName": "Auto"},
                  "modelSelectionHistory": ["default"], "version": 1}
        with open(cfg, "w") as f:
            json.dump(before, f, indent=2)
        code, out, err = self.run_bridge("say hi\n<hc from=\"t3 x\">/model composer-2.5</hc>\nsay bye\n", "--model", "claude-opus-5-5",
                                         env={"CURSOR_CONFIG_DIR": cfg_dir})
        self.assertEqual(code, 0, err)
        with open(cfg) as f:
            self.assertEqual(json.load(f), before)
        # the chat still runs on what was picked: both changes went through (one sent by another agent)
        self.assertEqual([r["model"] for r in self.transcript() if r["type"] == "model"],
                         ["claude-opus-5-5[context=300k,effort=medium,fast=false]", "composer-2.5[fast=true]"])

    def test_full_access_is_cursors_force(self):
        code, _, err = self.run_bridge("say hi\n", "--force")
        self.assertEqual(code, 0, err)
        self.assertEqual([r["argv"] for r in self.sent() if "argv" in r], [["--force", "acp"]])
        self.assertEqual(self.transcript()[0]["perm"], "full")

    def test_permission_goes_to_the_chat_card(self):
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        p.stdin.write("run touch made.txt\n")
        p.stdin.flush()
        req = self.wait_for(os.path.join(self.app, "data", "chat-asks", "req", "t9.*.json"))[0]
        with open(req) as f:
            r = json.load(f)
        # the hook's own shape: the card reads "Run: touch made.txt"
        self.assertEqual((r["tool_name"], r["tool_input"]["command"], r["hook_event_name"]), ("Bash", "touch made.txt", "PermissionRequest"))
        ans = req.replace("/req/", "/ans/")
        with open(ans, "w") as f:
            f.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}))
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertFalse(os.path.exists(req) or os.path.exists(ans))
        recs = self.transcript()
        done = [x for x in recs if x["type"] == "permission_done"][0]
        self.assertEqual((done["outcome"], done["by"], done["option"]), ("allowed", "chat", "allow-once"))
        self.assertIn("ALLOWED", [x for x in recs if x["type"] == "agent"][0]["text"])
        self.assertIn(("blocked", "approve Bash: touch made.txt", "permission", "cursor"), statuses(out))
        # allowed once, never always: the answer Cursor got
        answer = [x["got"] for x in self.sent() if "got" in x and x["got"].get("id") == 0 and "result" in x["got"]][0]
        self.assertEqual(answer["result"]["outcome"], {"outcome": "selected", "optionId": "allow-once"})

    def test_permission_rejected_in_the_terminal(self):
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        p.stdin.write("run rm -rf build\n")
        p.stdin.flush()
        req = self.wait_for(os.path.join(self.app, "data", "chat-asks", "req", "t9.*.json"))[0]
        p.stdin.write("3")
        p.stdin.flush()
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        aid = os.path.basename(req)[:-5]
        with open(os.path.join(self.app, "data", "chat-asks", "end", aid)) as f:
            self.assertEqual(f.read().strip(), "Declined or interrupted in the terminal")
        self.assertFalse(os.path.exists(req))
        recs = self.transcript()
        self.assertEqual([x["outcome"] for x in recs if x["type"] == "permission_done"], ["declined"])
        self.assertIn("REFUSED", [x for x in recs if x["type"] == "agent"][0]["text"])
        self.assertEqual([x["status"] for x in recs if x["type"] == "tool_done"], ["failed"])

    def test_no_terminal_no_card(self):
        """Outside a Herdr terminal no card is left (as ask-hook.sh): the terminal answers."""
        p = self.start()
        p.stdin.write("run ls\n")
        p.stdin.flush()
        time.sleep(1.5)
        p.stdin.write("1")
        p.stdin.flush()
        p.communicate("", timeout=30)
        self.assertFalse(os.path.exists(os.path.join(self.app, "data", "chat-asks", "req")) and os.listdir(os.path.join(self.app, "data", "chat-asks", "req")))
        self.assertEqual([x["by"] for x in self.transcript() if x["type"] == "permission_done"], ["terminal"])

    def test_resume_loads_the_same_chat(self):
        code, out, err = self.run_bridge("say more\n", "--resume", "abc")
        self.assertEqual(code, 0, err)
        loads = [x["got"] for x in self.sent() if x.get("got", {}).get("method") == "session/load"]
        self.assertEqual(loads[0]["params"]["sessionId"], "abc")
        self.assertFalse([x for x in self.sent() if x.get("got", {}).get("method") == "session/new"])
        recs = self.transcript("abc")
        self.assertTrue(recs[0]["resumed"])
        # the replayed chat isn't written again
        self.assertNotIn("EARLIER", json.dumps(recs))
        self.assertIn("(resumed)", out)

    def test_a_lost_chat_says_so(self):
        code, out, err = self.run_bridge("say hi\n", "--resume", "gone")
        self.assertEqual(code, 0, err)
        recs = self.transcript()
        self.assertEqual(recs[0]["type"], "error")
        self.assertIn("couldn't load chat gone", recs[0]["text"])
        self.assertFalse(recs[1]["resumed"])
        self.assertIn("couldn't load chat gone", out)

    def test_signed_out_stops_and_says_how(self):
        code, out, _ = self.run_bridge("say hi\n", env={"FAKE_ACP_SIGNED_OUT": "1"})
        self.assertEqual(code, 1)
        self.assertIn("cursor-agent login", out)
        self.assertIn("blocked", [s[0] for s in statuses(out)])
        self.assertEqual([s[2] for s in statuses(out) if s[0] == "blocked"], ["auth"])

    def test_esc_stops_a_turn(self):
        p = self.start()
        p.stdin.write("hang\n")
        p.stdin.flush()
        time.sleep(1.5)
        p.stdin.write("\x1b")
        p.stdin.flush()
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual([x["reason"] for x in self.transcript() if x["type"] == "turn_end"], ["cancelled"])
        self.assertTrue([x for x in self.sent() if x.get("got", {}).get("method") == "session/cancel"])

    def test_no_cursor_installed(self):
        code, out, _ = self.run_bridge("", env={"HERDR_CURSOR_BIN": "", "PATH": "/nonexistent"})
        self.assertEqual(code, 1)
        self.assertIn("isn't installed", out)


class Revive(unittest.TestCase):
    """An Assistant on Cursor's chat bridge, found after a crash (herdr-task revive): its argv and its chat id."""

    def run_py(self, code):
        env = {**os.environ, "SHEPHERDR_NO_MIGRATE": "1"}
        out = subprocess.run([sys.executable, "-c", "import sys, json; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]);" + code,
                              os.path.join(ROOT, "mods", "tasks", "bin")], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_bridge_argv_starts_again_on_herdr_task(self):
        got = self.run_py("from herdr_task.revive import base_argv, resume_argv;"
                          "k, a = base_argv(['/usr/bin/python3', '/h/.local/bin/herdr-task', 'cursor-chat', '--model', 'm', '--resume', 'OLD', '--force']);"
                          "print(json.dumps([k, a, resume_argv(k, a, 'NEW')]))")
        self.assertEqual(got, ["cursor", ["herdr-task", "cursor-chat", "--model", "m", "--force"],
                               ["herdr-task", "cursor-chat", "--model", "m", "--force", "--resume", "NEW"]])

    def test_its_transcript_names_the_chat(self):
        got = self.run_py("from herdr_task.sessions import CURSOR_BRIDGE;"
                          f"m = CURSOR_BRIDGE.search('/h/.local/share/shepherdr/cursor-chats/{SID}.jsonl'); print(json.dumps(m.group(1)))")
        self.assertEqual(got, SID)


if __name__ == "__main__":
    unittest.main()

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
        self.allow = os.path.join(self.dir, "allowlist")   # the stand-in's cli-config permissions.allow (SHEP-18)
        self.env = {**os.environ, "HERDR_CURSOR_BIN": fake, "HERDR_CURSOR_CHATS": self.chats, "FAKE_ACP_LOG": self.acp, "FAKE_ACP_ALLOW": self.allow,
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

    def test_auto_review_runs_as_ask_and_says_so(self):
        """SHEP-9: Cursor's ACP server ignores Auto-review (a real cursor-agent 2026.11.13 asked for `echo > file`
        with --auto-review and with approvalMode "auto-review"): the bridge never passes it on as if it worked, runs Ask
        and says so in its terminal and the chat."""
        code, out, err = self.run_bridge("say hi\n", "--auto-review")
        self.assertEqual(code, 0, err)
        self.assertEqual([r["argv"] for r in self.sent() if "argv" in r], [["acp"]])
        recs = self.transcript()
        self.assertEqual(recs[0]["perm"], "manual")
        self.assertIn("Auto-review isn't available in a Cursor chat", next(r["text"] for r in recs if r["type"] == "error"))
        self.assertIn("Auto-review isn't available", out)
        # Full access stays itself: no note
        code, out, err = self.run_bridge("say hi\n", "--force", "--auto-review")
        self.assertNotIn("Auto-review isn't available", out)

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
            self.assertEqual(f.read().strip(), "Rejected in the terminal")
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

    # ---- SHEP-17: permissions that wait together, keys that aren't answers, words sent while it asks ----

    def asks(self, n):
        """The n card requests waiting now, oldest first: [(id, request)]."""
        end = time.time() + 15
        while time.time() < end:
            hits = sorted(glob.glob(os.path.join(self.app, "data", "chat-asks", "req", "t9.*.json")))
            if len(hits) >= n:
                out = []
                for h in hits:
                    with open(h) as f:
                        out.append((os.path.basename(h)[:-5], json.load(f)))
                return out
            time.sleep(0.05)
        self.fail(f"{n} asks never showed")

    def answered(self):
        """The bridge's answers to Cursor's permission requests: {request id: outcome}."""
        return {x["got"]["id"]: x["got"]["result"]["outcome"] for x in self.sent()
                if "got" in x and "result" in x["got"] and "method" not in x["got"] and "outcome" in (x["got"]["result"] or {})}

    def card(self, aid, decision):
        with open(os.path.join(self.app, "data", "chat-asks", "ans", aid + ".json"), "w") as f:
            f.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}))

    def key(self, p, data, pause=0.4):
        p.stdin.write(data)
        p.stdin.flush()
        time.sleep(pause)   # one read each, as a person's keys come

    def test_two_asks_at_once_each_answered_under_its_own_id(self):
        """The real case: Cursor asked for a web fetch and a `gh` call in one turn, before either was answered. The
        second overwrote the first, which was never answered: the turn waited forever. Each is kept now, with its
        own card, and the terminal's keys answer the oldest one's menu."""
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "both gh pr view 1\n")
        (fetch_id, fetch), (shell_id, shell) = self.asks(2)
        self.assertNotEqual(fetch_id, shell_id)   # asked in the same second, each its own card
        self.assertEqual((fetch["tool_name"], shell["tool_name"], shell["tool_input"]["command"]), ("WebFetch", "Bash", "gh pr view 1"))
        self.assertEqual(fetch["tool_input"]["url"], "https://example.com/thread")   # the card reads "Fetch example.com"
        # the later card answers its own request, though the menu shows the earlier one
        self.card(shell_id, {"behavior": "allow"})
        time.sleep(0.8)
        self.assertEqual(self.answered(), {101: {"outcome": "selected", "optionId": "allow-once"}})
        # the terminal's 3 rejects the menu's own request: the fetch, never the shell call
        self.key(p, "3")
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.answered(), {100: {"outcome": "selected", "optionId": "reject-once"}, 101: {"outcome": "selected", "optionId": "allow-once"}})
        recs = self.transcript()
        self.assertIn("FETCH:reject-once SHELL:allow-once", [x for x in recs if x["type"] == "agent"][0]["text"])
        self.assertEqual(sorted((x["outcome"], x["by"]) for x in recs if x["type"] == "permission_done"), [("allowed", "chat"), ("declined", "terminal")])
        with open(os.path.join(self.app, "data", "chat-asks", "end", fetch_id)) as f:
            self.assertEqual(f.read().strip(), "Rejected in the terminal")
        self.assertIn("Cursor also asks:", out)
        self.assertIn("Allowed once in the chat: gh pr view 1", out)
        self.assertEqual(glob.glob(os.path.join(self.app, "data", "chat-asks", "req", "*")), [])

    def test_menu_keys_answer_in_order_and_say_what_always_adds(self):
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "both hc read t5\n")
        self.asks(2)
        self.key(p, "1")    # the fetch, allowed once
        self.key(p, "2")    # then the shell call's menu: Allow always, what Cursor adds said plainly
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.answered(), {100: {"outcome": "selected", "optionId": "allow-once"}, 101: {"outcome": "selected", "optionId": "allow-always"}})
        self.assertIn("2. Allow always \x1b[2m(adds Shell(hc) to Cursor's allowlist, for every such command)", out)
        self.assertIn("Allowed always (adds Shell(hc) to Cursor's allowlist, for every such command): hc read t5", out)
        kinds = [x["kind"] for x in self.transcript() if x["type"] == "permission_done"]
        self.assertEqual(kinds, ["allow_once", "allow_always"])

    # ---- Allow always from the chat's card (SHEP-18) ----

    def saved(self):
        """What the stand-in Cursor saved in its allowlist (Allow always), in order."""
        try:
            with open(self.allow) as f:
                return f.read().split()
        except OSError:
            return []

    def test_card_allow_always_picks_cursors_exact_option_and_saves_as_the_terminal(self):
        """The card offered only Allow (once): João's "always approve" was only in the terminal. The request now carries
        Cursor's own Allow always, its exact option and what it adds, and the card's answer picks that option of that
        request: Cursor saves the same as with the terminal's 2."""
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "run gh pr view 1\n")
        [(aid, req)] = self.asks(1)
        self.assertEqual(req["cursor_always"], {"option": "allow-always", "name": "Allow always",
                                                "scope": "adds Shell(gh) to Cursor's allowlist, for every such command"})
        self.card(aid, {"behavior": "allow", "cursorOption": "allow-always"})
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.answered(), {0: {"outcome": "selected", "optionId": "allow-always"}})
        self.assertEqual(self.saved(), ["Shell(gh)"])
        done = [x for x in self.transcript() if x["type"] == "permission_done"]
        self.assertEqual([(x["outcome"], x["by"], x["option"], x["kind"]) for x in done], [("allowed", "chat", "allow-always", "allow_always")])
        self.assertIn("Allowed always in the chat (adds Shell(gh) to Cursor's allowlist, for every such command): gh pr view 1", out)
        self.assertEqual(glob.glob(os.path.join(self.app, "data", "chat-asks", "*", aid + "*")), [])
        # the terminal's own 2 on the same request saves exactly the same
        os.remove(self.allow)
        os.remove(self.acp)
        q = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(q, "run gh pr view 1\n")
        self.asks(1)
        self.key(q, "2")
        out, err = q.communicate("", timeout=30)
        self.assertEqual(q.returncode, 0, err)
        self.assertEqual(self.answered(), {0: {"outcome": "selected", "optionId": "allow-always"}})
        self.assertEqual(self.saved(), ["Shell(gh)"])

    def test_card_allow_once_saves_nothing(self):
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "run gh pr view 1\n")
        [(aid, _)] = self.asks(1)
        self.card(aid, {"behavior": "allow"})
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.answered(), {0: {"outcome": "selected", "optionId": "allow-once"}})
        self.assertEqual(self.saved(), [])

    def test_a_card_always_this_request_didnt_offer_is_never_allowed(self):
        """Another option's id (Allow once's, one Cursor never sent), or Allow always where Cursor offered none: nothing
        is allowed from the card, not even once; its card closes saying so and the terminal's menu still answers."""
        for prompt, oid in (("run gh pr view 1", "allow-once"), ("run gh pr view 1", "allow-always-2"), ("plain gh pr view 1", "allow-always")):
            with self.subTest(prompt=prompt, option=oid):
                for f in (self.acp, self.allow):
                    if os.path.exists(f):
                        os.remove(f)
                p = self.start(env={"HC_TERMINAL_ID": "t9"})
                self.key(p, prompt + "\n")
                [(aid, req)] = self.asks(1)
                if prompt.startswith("plain"):
                    self.assertNotIn("cursor_always", req)    # no Allow always offered: the card has none
                self.card(aid, {"behavior": "allow", "cursorOption": oid})
                end = self.wait_for(os.path.join(self.app, "data", "chat-asks", "end", aid))[0]
                self.assertEqual(self.answered(), {})
                with open(end) as f:
                    self.assertIn("didn't match", f.read())
                self.assertEqual(glob.glob(os.path.join(self.app, "data", "chat-asks", "req", aid + ".json")), [])
                self.key(p, "1")   # Allow once, in the terminal
                out, err = p.communicate("", timeout=30)
                self.assertEqual(p.returncode, 0, err)
                rid = 300 if prompt.startswith("plain") else 0
                self.assertEqual(self.answered(), {rid: {"outcome": "selected", "optionId": "allow-once"}})
                self.assertEqual(self.saved(), [])

    def test_no_card_always_where_what_it_adds_cant_be_said(self):
        """A shell call whose reason names nothing (Cursor saves nothing then): its terminal menu keeps Allow always,
        saying it can't tell; the card offers none."""
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "noreason gh pr view 1\n")
        [(aid, req)] = self.asks(1)
        self.assertNotIn("cursor_always", req)
        self.key(p, "3")
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("2. Allow always \x1b[2m(what Cursor saves in its allowlist for it isn't shown in this request)", out)

    def test_two_asks_at_once_each_card_always_or_once_its_own(self):
        """Two at once: the fetch's card allowed always, the shell call's once; each answered under its own request id
        with its own option, and only the fetch's site saved."""
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "both gh pr view 1\n")
        (fetch_id, fetch), (shell_id, shell) = self.asks(2)
        self.assertEqual(fetch["cursor_always"]["scope"], "adds WebFetch(example.com) to Cursor's allowlist, for every fetch from that site")
        self.assertEqual(shell["cursor_always"]["scope"], "adds Shell(gh) to Cursor's allowlist, for every such command")
        self.card(shell_id, {"behavior": "allow"})
        self.card(fetch_id, {"behavior": "allow", "cursorOption": "allow-always"})
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.answered(), {100: {"outcome": "selected", "optionId": "allow-always"}, 101: {"outcome": "selected", "optionId": "allow-once"}})
        self.assertEqual(self.saved(), ["WebFetch(example.com)"])
        self.assertIn("FETCH:allow-always SHELL:allow-once", [x for x in self.transcript() if x["type"] == "agent"][0]["text"])

    def test_a_late_card_answer_for_a_request_cursor_gave_up_on_does_nothing(self):
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "drop gh pr view 1\n")
        ends = self.wait_for(os.path.join(self.app, "data", "chat-asks", "end", "t9.*"))
        self.assertEqual(len(ends), 1)
        self.assertEqual(glob.glob(os.path.join(self.app, "data", "chat-asks", "req", "*")), [])
        aid = os.path.basename(ends[0])
        self.card(aid, {"behavior": "allow", "cursorOption": "allow-always"})
        time.sleep(0.8)
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual([o for o in self.answered().values() if o.get("optionId")], [])
        self.assertEqual(self.saved(), [])

    def test_a_paste_enter_or_reports_never_answer_and_a_message_waits(self):
        """A message sent while it asks (hc's paste, then Enter) once rejected the permission and was lost, and a
        pasted digit could pick an option: now the words wait their turn and only a key of its own answers."""
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "run touch made.txt\n")
        ((aid, _),) = self.asks(1)
        self.key(p, "\x1b[200~say 2\x1b[201~\r")   # hc send "say 2": not an answer, not a reject
        self.key(p, "\r")                          # Enter alone picks nothing
        self.key(p, "\x1b[<65;101;61M")             # a wheel report
        self.key(p, "\x1b[I")                       # a focus report
        self.key(p, "\x1b[A")                       # an arrow
        self.key(p, "2 more")                       # words typed: a digit among them is no answer...
        self.key(p, "\x7f" * 6)                     # ...deleted again
        self.assertEqual(self.answered(), {})
        self.assertTrue(os.path.exists(os.path.join(self.app, "data", "chat-asks", "req", aid + ".json")))
        self.card(aid, {"behavior": "allow"})
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.answered(), {0: {"outcome": "selected", "optionId": "allow-once"}})
        recs = self.transcript()
        # the words went once the turn ended, as the person's next message
        self.assertEqual([x["text"] for x in recs if x["type"] == "user"], ["run touch made.txt", "say 2"])
        self.assertEqual([x["text"] for x in recs if x["type"] == "agent"], ["ALLOWED", "2"])
        self.assertIn("Press 1, 2, 3 to answer", out)

    def test_esc_and_ctrl_c_alone_reject(self):
        for k in ("\x1b", "\x03"):
            with self.subTest(key=repr(k)):
                if os.path.exists(self.acp):
                    os.remove(self.acp)
                p = self.start(env={"HC_TERMINAL_ID": "t9"})
                self.key(p, "run rm -rf build\n")
                self.asks(1)
                self.key(p, k)
                _, err = p.communicate("", timeout=30)
                self.assertEqual(p.returncode, 0, err)
                self.assertEqual(self.answered(), {0: {"outcome": "selected", "optionId": "reject-once"}})

    def test_words_written_instead_of_the_card_reach_cursor(self):
        """The window's chat answers a card with the person's words as a denial's message (cards.js replyWith); Cursor's
        reject can't carry them, so they go as the next message instead of being lost."""
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "run rm -rf build\n")
        ((aid, _),) = self.asks(1)
        self.card(aid, {"behavior": "deny", "message": "The person didn't use the card and wrote in the chat instead: say not that"})
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.answered(), {0: {"outcome": "selected", "optionId": "reject-once"}})
        recs = self.transcript()
        self.assertEqual([x["text"] for x in recs if x["type"] == "user"], ["run rm -rf build", "say not that"])
        self.assertIn("Rejected in the chat: rm -rf build", out)

    def test_a_request_cursor_gave_up_on_closes_its_card(self):
        p = self.start(env={"HC_TERMINAL_ID": "t9"})
        self.key(p, "drop ls\n", pause=1.5)
        self.key(p, "say still here\n")
        out, err = p.communicate("", timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.answered(), {200: {"outcome": "cancelled"}})
        self.assertEqual(glob.glob(os.path.join(self.app, "data", "chat-asks", "req", "*")), [])
        ends = glob.glob(os.path.join(self.app, "data", "chat-asks", "end", "t9.*"))
        with open(ends[0]) as f:
            self.assertEqual(f.read().strip(), "Cursor stopped waiting on it")
        recs = self.transcript()
        self.assertEqual([x["outcome"] for x in recs if x["type"] == "permission_done"], ["stale"])
        self.assertIn("still here", [x["text"] for x in recs if x["type"] == "agent"][-1])

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


class AlwaysScope(unittest.TestCase):
    """What the card says Allow always adds, for each request Cursor's ACP server sends (cursor-agent 2026.11.17's
    formatOperation, requestWebPermission and subagent prompts), as it saves it; none where the request can't say."""

    @classmethod
    def setUpClass(cls):
        sys.dont_write_bytecode = True
        sys.path.insert(0, os.path.join(ROOT, "mods", "tasks", "bin"))
        from herdr_task import cursor_chat
        cls.cc = cursor_chat

    def offer(self, tool, options=None):
        cc = self.cc
        req = cc.hook_request(tool, "/w", "s")
        a = {"options": options or [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                                    {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
                                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}],
             "req": req, "title": cc.strip_ticks(tool.get("title")) or req["tool_name"],
             "why": req["tool_input"].get("description") or req["tool_input"].get("why") or "", "kind": tool.get("kind") or "other"}
        o = cc.always_offer(a)
        return o["scope"] if o else None

    def test_each_kind(self):
        text = lambda t: [{"type": "content", "content": {"type": "text", "text": t}}]
        cases = [
            ({"title": "`gh pr view 1 | jq .`", "kind": "execute", "content": text("Not in allowlist: gh, jq")},
             "adds Shell(gh), Shell(jq) to Cursor's allowlist, for every such command"),
            ({"title": "`git commit -m x`", "kind": "execute", "content": text("Not in allowlist: git commit")},
             "adds Shell(git commit) to Cursor's allowlist, for every such command"),
            ({"title": "Write /w/new.txt", "kind": "edit", "content": [{"type": "diff", "path": "/w/new.txt", "oldText": None, "newText": "hi"}]},
             "adds Write(/w/new.txt) to Cursor's allowlist, for every change to that file"),
            ({"title": "Edit `/w/a.py`", "kind": "edit", "content": [{"type": "diff", "path": "/w/a.py", "oldText": "a", "newText": "b"}]},
             "adds Write(/w/a.py) to Cursor's allowlist, for every change to that file"),
            ({"title": "Delete `/w/old.txt`", "kind": "edit"}, "adds Write(/w/old.txt) to Cursor's allowlist, for every change to that file"),
            ({"title": "Read `/etc/hosts`", "kind": "read"}, "adds Read(/etc/hosts) to Cursor's allowlist, for every read of that file"),
            ({"title": "Fetch https://Docs.Example.com:8443/a?b", "kind": "fetch"},
             "adds WebFetch(docs.example.com) to Cursor's allowlist, for every fetch from that site"),
            ({"title": "Web search: cursor acp permissions", "kind": "search"},
             "adds WebSearch(cursor acp permissions) to Cursor's allowlist, for that same search"),
            ({"title": "github: create_issue", "kind": "other", "content": text('```json\n{"title": "x"}\n```')},
             "adds this tool (github: create_issue) to Cursor's allowlist, for every call to it"),
        ]
        for tool, want in cases:
            with self.subTest(title=tool["title"]):
                self.assertEqual(self.offer(tool), want)

    def test_a_subagent_fetch_names_its_site_in_the_option(self):
        opts = [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow-always", "name": "Always allow example.com", "kind": "allow_always"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]
        tool = {"title": "Allow web fetch?", "kind": "other", "content": [{"type": "content", "content": {"type": "text", "text": "URL: https://example.com/x"}}]}
        self.assertEqual(self.offer(tool, opts), "adds WebFetch(example.com) to Cursor's allowlist, for every fetch from that site")

    def test_none_where_it_cant_be_said_or_isnt_offered(self):
        cases = [
            {"title": "`echo hi`", "kind": "execute"},                                         # no "Not in allowlist"
            {"title": "`x`", "kind": "execute", "content": [{"type": "content", "content": {"type": "text", "text": "Parser failed to parse command (possible bypass)"}}]},
            {"title": "Web search: ", "kind": "search"},                                       # Cursor saves nothing
            {"title": "Fetch ", "kind": "fetch"},
            {"title": "Allow web search?", "kind": "other"},                                   # a subagent's: never offered
            {"title": "Unknown operation", "kind": "other"},
            {"title": "Something", "kind": "think"},
        ]
        for tool in cases:
            with self.subTest(title=tool["title"]):
                self.assertIsNone(self.offer(tool))
        once = [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}, {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]
        self.assertIsNone(self.offer({"title": "`gh x`", "kind": "execute", "content": [{"type": "content", "content": {"type": "text", "text": "Not in allowlist: gh"}}]}, once))

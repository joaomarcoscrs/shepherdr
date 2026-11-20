import home_guard

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mods" / "tasks" / "bin"))
from herdr_task import revive, sessions


def session_id(n):
    return f"20000000-0000-4000-8000-{n:012d}"


class CodexSessionDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.directory = root / "sessions" / "2026" / "01" / "01"
        self.directory.mkdir(parents=True)
        self.guard = self.rollout(1, source={"subagent": {"other": "guardian"}})
        self.sibling = self.rollout(2, source={"subagent": "review"}, parent_thread_id=session_id(3))
        self.main = self.rollout(3, source="cli", forked_from_thread_id=session_id(5))
        self.helper = self.rollout(4, source="cli")
        self.open = {"54000": [self.guard, self.sibling, self.main], "54001": [self.helper]}
        for change in (
            patch.multiple(sessions, HOME=str(root), kids=lambda pid: ["54001"] if pid == 54000 else [],
                           _open_files=lambda pid: self.open.get(pid, [])),
            patch.dict(os.environ, {"CODEX_HOME": str(root)}),
        ):
            change.start()
            self.addCleanup(change.stop)
        self.term = {
            "address": "t1", "state": "live", "pid": 54000, "cwd": "/project",
            "argv": ["codex", "fork", session_id(5), "--approve-for-me"],
            "foreground": {"pid": 54000, "name": "codex", "cwd": "/project"},
            "status": {"app": "codex", "state": "idle"},
            "labels": {"herdr.role": "coordinator", revive.LABEL: json.dumps({
                "kind": "codex", "session": session_id(1), "log": self.guard, "pid": 54000,
                "argv": ["codex", "--approve-for-me"],
            })},
        }

    def rollout(self, n, **meta):
        path = self.directory / f"rollout-2026-01-01T00-00-00-{session_id(n)}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": session_id(n), **meta}}) + "\n")
        return str(path)

    def tick_label(self):
        sent = []

        def write(term, record):
            term["labels"][revive.LABEL] = json.dumps(record)
            sent.append(record)

        with patch.multiple(revive, hc_json=lambda *args: {"terminals": [self.term]} if args[0] == "ls" else self.term,
                            write_record=write):
            revive.revive_pass({})
        self.assertEqual(len(sent), 1)
        return sent[0]

    def test_live_tick_replaces_a_guardian_label_and_task_record_with_the_parent_fork(self):
        self.assertEqual(sessions.session_of(54000), (session_id(3), self.main))
        record = self.tick_label()
        self.assertEqual((record["session"], record["log"]), (session_id(3), self.main))
        self.assertEqual(record["argv"], ["codex", "--approve-for-me"])
        store = SimpleNamespace(dirty=False)
        agent = {"address": "t1", "agent": "codex", "session": session_id(1), "log": self.guard}
        with patch.object(sessions, "detail", lambda term: term):
            sessions.refresh_sessions(store, {"agents": [agent]}, [self.term])
        self.assertTrue(store.dirty)
        self.assertEqual((agent["session"], agent["log"]), (session_id(3), self.main))

        # When the original process is gone, a repair from before the crash still resumes the fork.
        recovered = dict(self.term, pid=54010, argv=["/bin/bash"])
        how, args, _ = revive.plan(recovered, record, set())
        self.assertEqual(how, "resume")
        self.assertEqual(args, ["codex", "resume", "--approve-for-me", session_id(3)])

    def test_guardian_only_clears_the_saved_target_until_the_parent_appears(self):
        self.open["54000"] = [self.guard, self.sibling]
        self.assertEqual(sessions.session_of(54000), (None, None))
        bad = revive.read_record(self.term)
        how, args, _ = revive.plan(dict(self.term, pid=54010), bad, {session_id(1)})
        self.assertEqual(how, "fresh")
        self.assertNotIn(session_id(1), args)
        cleared = self.tick_label()
        self.assertIsNone(cleared["session"])
        self.assertIsNone(cleared["log"])
        self.open["54000"] = [self.sibling, self.main, self.guard, self.main]
        self.assertEqual(self.tick_label()["session"], session_id(3))
        self.open["54000"] = [self.helper, self.main, self.guard]
        self.assertEqual(sessions.session_of(54000), (None, None))

    def test_disk_recovery_requires_a_matching_parent_header_and_keeps_explicit_helpers(self):
        self.assertEqual(sessions.session_of(54001), (session_id(4), self.helper))
        for n in (1, 2):
            self.assertIsNone(sessions.log_by_session("codex", session_id(n)))
        self.assertIsNone(sessions.find_log({"agent": "codex", "session": session_id(1), "log": self.guard}, "SYN-1", []))
        self.assertEqual(sessions.find_log({"agent": "codex", "session": session_id(3)}, "SYN-1", []), self.main)
        wrong_id = self.rollout(6, id=session_id(5), source="cli")
        self.assertIsNone(sessions.codex_parent_id(wrong_id))
        parent_id = self.rollout(7, source="cli", parent_thread_id=session_id(3))
        self.assertIsNone(sessions.codex_parent_id(parent_id))

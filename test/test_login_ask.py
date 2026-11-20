"""Sign-in waiter using a fake core and a private Ask spool."""
import home_guard  # noqa: F401
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / "mods" / "tasks" / "bin"
LAUNCH = """
import os, shutil
from herdr_task import base, cli, login
assert shutil.which("hc") == os.environ["FIXTURE_HC"]
base.guard = lambda args: None
login.ROOT = os.environ["FIXTURE_BROWSER"]
login.browser_path = lambda: "fixture"
raise SystemExit(cli.main())
"""


class LoginAsk(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.term = self.path / "terminal.json"
        self.term.write_text(json.dumps({"address": "t5", "state": "live", "created_ms": 10, "labels": {}}))
        self.spool = self.path / "data" / "chat-asks"
        fake = self.path / "hc"
        fake.write_text('#!/bin/sh\ncase "$1" in get) cat "$FIXTURE_TERM";; label) exit 0;; *) exit 1;; esac\n')
        fake.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.path) + os.pathsep + os.environ["PATH"],
                    "HC_TERMINAL_ID": "t5", "FIXTURE_HC": str(fake), "FIXTURE_TERM": str(self.term),
                    "FIXTURE_BROWSER": folder.name, "PYTHONPATH": str(PACKAGE), "HERDR_APP_HOME": folder.name}

    def start(self, *args):
        p = subprocess.Popen(["python3", "-c", LAUNCH, "ask-login", *args], env=self.env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, p)
        return p

    @staticmethod
    def stop(p):
        if p.poll() is None:
            p.terminate()
            p.communicate(timeout=6)

    def request(self):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            requests = list((self.spool / "req").glob("*.json"))
            if requests:
                self.assertEqual(len(requests), 1)
                return requests[0]
            time.sleep(0.02)
        self.fail("No Ask was posted")

    def finish(self, p, expect):
        out, err = p.communicate(timeout=6)
        self.assertEqual(p.returncode, expect, (out, err))
        return out + err

    def test_chrome_handoff_waits_for_confirmation_without_browser(self):
        p = self.start("--site", "https://APP.EXAMPLE.INVALID:8443")
        req = self.request()
        data = json.loads(req.read_text())
        self.assertEqual(data["tool_input"], {"kind": "site", "origin": "https://app.example.invalid:8443"})
        self.assertEqual(data["owner"], {"created": 10, "task": None})
        self.assertIsNone(p.poll())
        extra = self.start("--terminal")
        self.assertIn("already waiting", self.finish(extra, 1))
        (self.spool / "ans" / req.name).write_text('{"login":"confirmed"}')
        self.assertIn("Verify the sign-in", self.finish(p, 0))
        self.assertFalse(req.exists())
        self.assertFalse((self.spool / "ans" / req.name).exists())

    def test_legacy_browser_uses_own_marker_and_cancel(self):
        self.term.write_text(json.dumps({"address": "t5", "state": "live", "created_ms": 10,
                                         "labels": {"herdr.task": "example#3"}}))
        self.env["HERDR_BROWSER_TASK"] = "example#2"
        self.assertIn("owns this task", self.finish(self.start("--browser"), 1))
        self.env["HERDR_BROWSER_TASK"] = "example#3"
        (self.path / "example-3.task").write_text("example#3")
        p = self.start("--browser")
        req = self.request()
        self.assertEqual(json.loads(req.read_text())["tool_input"], {"kind": "browser"})
        self.assertTrue((self.path / "example-3.used").exists())
        (self.spool / "ans" / req.name).write_text('{"login":"canceled"}')
        self.assertIn("canceled", self.finish(p, 1))
        self.assertFalse(req.exists())

    def test_cli_session_closes_when_replaced_or_interrupted(self):
        p = self.start("--terminal")
        req = self.request()
        self.assertEqual(json.loads(req.read_text())["tool_input"], {"kind": "terminal"})
        new = self.path / "new.json"
        new.write_text(json.dumps({"address": "t5", "state": "live", "created_ms": 11, "labels": {}}))
        new.replace(self.term)
        self.finish(p, 1)
        self.assertFalse(req.exists())
        self.assertIn("session ended", (self.spool / "end" / req.stem).read_text())
        p = self.start("--terminal")
        req = self.request()
        p.terminate()
        self.finish(p, 1)
        self.assertFalse(req.exists())

    def test_malformed_or_secret_url_never_reaches_ask_store(self):
        for url in ("javascript:alert(1)", "https://example.invalid:90000", "https://a:b@example.invalid",
                    "https://example.invalid/auth/code", "https://example.invalid?token=redacted", "https://example.invalid#redacted"):
            with self.subTest(url=url):
                self.assertNotIn(url, self.finish(self.start("--site", url), 1))
        self.assertFalse(self.spool.exists())

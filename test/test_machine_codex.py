"""The sealed Codex usage reader, with a fictional CLI and sign-ins."""
import home_guard  # noqa: F401
import base64
import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(home_guard.ROOT, "mods", "tasks", "bin"))
from herdr_task import machine

NOW = 1795444440000
AUTH_CLAIMS = "https://api.openai.com/auth"


def jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "fixture.%s.fixture" % encoded


class CodexUsage(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "named-profile"
        self.home.mkdir()
        self.seal = Path(folder.name) / "seal"
        self.seal.mkdir()
        self.binary = Path(folder.name) / "codex"
        self.binary.write_text(
            "#!" + sys.executable + "\n" + r'''
import json, os, sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print(os.environ["FAKE_CODEX_VERSION"])
    sys.exit()
assert sys.argv[1] == "app-server"
mode = os.environ["FAKE_CODEX_MODE"]
home = Path(os.environ["CODEX_HOME"])
assert Path("proof-of-seal").is_file()
account = {"type": "chatgpt", "email": "reader@example.invalid", "planType": "plus"}
replies = {
    "initialize": {},
    "config/read": {"config": {"cli_auth_credentials_store": "auto" if mode == "keyring" else "file"}},
    "account/read": {"account": account, "requiresOpenaiAuth": True, "userId": "fake-user"},
    "account/rateLimits/read": {"accountId": "fake-workspace", "rateLimitsByLimitId": {
        "codex": {"limitId": "codex", "primary": {"usedPercent": 27, "windowDurationMins": 300, "resetsAt": 1795448000}},
        "fast": {"limitId": "fast", "limitName": "Fast", "secondary": {
            "usedPercent": 63, "windowDurationMins": 10080, "resetsAt": 1795800000}},
    }},
}
if mode == "other-user":
    replies["account/read"]["userId"] = "another-user"
if mode == "other-workspace":
    replies["account/rateLimits/read"]["accountId"] = "another-workspace"
if mode in {"api-key", "signed-out"}:
    replies["account/read"]["account"] = {"type": "apiKey"} if mode == "api-key" else None
if mode == "signed-out":
    replies["account/read"]["userId"] = None

for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    method = req["method"]
    if method == "account/read":
        assert req["params"]["refreshToken"] is False
    if method == "account/rateLimits/read":
        assert (home / "auth.json").is_symlink()
        assert os.environ.get("CODEX_REFRESH_TOKEN_URL_OVERRIDE") == "file:///dev/null"
        assert mode not in {"api-key", "signed-out", "keyring", "other-user", "account-error"}
        if mode == "switch-during-read":
            Path(os.environ["FAKE_SELECTED_HOME"], "auth.json").unlink()
    if (method, mode) in {("account/read", "account-error"), ("account/rateLimits/read", "transport"),
                           ("account/rateLimits/read", "rejected")}:
        message = "upstream auth service unavailable" if mode != "rejected" else "HTTP 401 Unauthorized"
        print(json.dumps({"id": req["id"], "error": {"code": -32603, "message": message}}), flush=True)
    else:
        print(json.dumps({"id": req["id"], "result": replies[method]}), flush=True)
''')
        self.binary.chmod(0o700)
        (self.seal / "proof-of-seal").touch()
        self.mock(patch.dict(os.environ, {"CODEX_HOME": str(self.home), "FAKE_SELECTED_HOME": str(self.home),
                                         "FAKE_CODEX_MODE": "ok", "FAKE_CODEX_VERSION": "codex-cli 0.171.0"}))
        self.mock(patch.object(machine, "which", return_value=str(self.binary)))
        self.mock(patch.object(machine, "_no_seal", return_value=None))
        self.mock(patch.object(machine, "now_ms", return_value=NOW))
        self.mock(patch.object(machine, "MAC", True))
        self.mock(patch.object(machine, "_mac_seal", return_value=(
            str(self.seal), lambda argv: argv, lambda: shutil.rmtree(self.seal / "codex"))))

    def mock(self, patcher):
        self.addCleanup(patcher.stop)
        patcher.start()

    def sign_in(self, expires_in=7200, age_days=10):
        last = datetime.datetime.fromtimestamp(NOW / 1000 - age_days * 86400, datetime.timezone.utc)
        data = {"auth_mode": "chatgpt", "last_refresh": last.isoformat(), "tokens": {
            "access_token": jwt({"exp": NOW // 1000 + expires_in}),
            "id_token": jwt({"email": "reader@example.invalid", AUTH_CLAIMS: {"chatgpt_user_id": "fake-user"}}),
            "refresh_token": "fictional-refresh",
            "account_id": "fake-workspace",
        }}
        (self.home / "auth.json").write_text(json.dumps(data))

    def test_reads_valid_chatgpt_after_seven_days(self):
        self.sign_in()
        out = machine.codex()
        self.assertEqual(out["state"], "ok", out)
        self.assertEqual(out["account"], "reader@example.invalid")
        self.assertEqual(out["plan"], "plus")
        self.assertEqual(out["windows"], [
            {"name": "5-hour", "used": 27, "resets_at": 1795448000000},
            {"name": "Fast · Weekly", "used": 63, "resets_at": 1795800000000},
        ])

    def test_preserves_conservative_guard_for_unverified_cli(self):
        self.sign_in()
        with patch.dict(os.environ, FAKE_CODEX_VERSION="codex-cli 0.170.0"), patch.object(machine, "_ask_codex") as ask:
            self.assertIn("Codex", machine.codex()["why"])
            ask.assert_not_called()

    def test_expired_and_near_expiry_are_distinct(self):
        for seconds, reason in ((1800, "expires soon"), (-1, "has expired")):
            with self.subTest(seconds=seconds):
                self.sign_in(expires_in=seconds)
                with patch.object(machine, "_ask_codex") as ask:
                    self.assertIn(reason, machine.codex()["why"])
                    ask.assert_not_called()

    def test_explicit_no_plan_and_signed_out(self):
        for mode, state in (("api-key", "no-plan"), ("signed-out", "signed-out")):
            with self.subTest(mode=mode), patch.dict(os.environ, FAKE_CODEX_MODE=mode):
                self.assertEqual(machine.codex()["state"], state)

    def test_unverified_accounts_never_supply_usage(self):
        for mode in ("other-user", "other-workspace", "switch-during-read", "keyring"):
            self.sign_in()
            with self.subTest(mode=mode), patch.dict(os.environ, FAKE_CODEX_MODE=mode):
                out = machine.codex()
                self.assertEqual(out["state"], "unknown", out)
                self.assertNotIn("codex_scope", out)

    def test_same_account_failures_are_scoped(self):
        self.sign_in()
        good = machine.codex()
        for mode, reason in (("transport", "try"), ("account-error", "verify"), ("rejected", "Sign in")):
            with self.subTest(mode=mode), patch.dict(os.environ, FAKE_CODEX_MODE=mode):
                out = machine.codex()
                self.assertEqual(out["state"], "unknown", out)
                self.assertIn(reason, out["why"])
                self.assertEqual(out["codex_scope"], good["codex_scope"])

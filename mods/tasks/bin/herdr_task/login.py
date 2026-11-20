"""The agent-side waiter for an explicit sign-in Ask. It uses the permission cards' spool."""
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.parse import urlsplit

from .base import Fail, HOME, run
from .browsers import ROOT, _task, browser_path

TOOL = "HerdrLogin"
WAIT_SECONDS = 1800
TERMINAL = re.compile(r"t[1-9][0-9bcdfghjkmnpqrstvwxyz]*")
# Only an HTTP(S) origin. Redirect paths, even without a query, can carry secrets.
ORIGIN = re.compile(r"https?://(?:[a-z0-9.-]+|\[[0-9a-f:]+\])(?::[0-9]{1,5})?/?", re.I | re.ASCII)


def ask_dir():
    return Path(os.environ.get("HERDR_APP_HOME") or Path(HOME, ".config", "herdr-app"), "data", "chat-asks")


def safe_origin(value):
    if len(value) > 253 or not ORIGIN.fullmatch(value):
        raise Fail("Use an HTTP(S) site origin only, such as https://app.example.com.")
    try:
        url = urlsplit(value)
        if url.port == 0 or not url.hostname or not url.hostname.strip("."):
            raise ValueError
    except ValueError:
        raise Fail("Use a valid HTTP(S) site origin.") from None
    return value.lower().rstrip("/")


def owner(address):
    try:
        p = run(["hc", "get", address, "--json"], timeout=4)
        t = json.loads(p.stdout) if p.returncode == 0 else None
    except (Fail, ValueError, OSError, subprocess.SubprocessError):
        return None
    if not isinstance(t, dict) or t.get("address") != address or t.get("state") != "live":
        return None
    labels = t.get("labels") or {}
    if labels.get("herdr.role") in ("reviewer", "helper") or not t.get("created_ms"):
        return None
    key = labels.get("herdr.task") or None
    if key and not re.fullmatch(r"[\w.@-]+#[0-9]+", key):
        return None
    return {"created": t["created_ms"], "task": key}


def browser_marker(task):
    if not task or os.environ.get("HERDR_BROWSER_TASK") != task or not browser_path():
        raise Fail("Use the task browser from its owning worker, or ask with --site for Chrome.")
    name = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9_-]", "-", task)).strip("-")
    marker = Path(ROOT, name + ".task")
    try:
        if marker.read_text().strip() == task:
            return marker.with_suffix(".used")
    except OSError:
        pass
    raise Fail("This task has no browser open. Use --site to ask for sign-in in Chrome.")


def task_closed(key):
    if not key:
        return False
    found, task = _task(key)
    return found and (task is None or task.get("state") in ("done", "awaiting", "deploying"))


def cmd_login(args):
    address = os.environ.get("HC_TERMINAL_ID", "")
    if not TERMINAL.fullmatch(address) or not (mine := owner(address)):
        raise Fail("Ask from your own live Herdr terminal.")
    if mine["task"] != (os.environ.get("HERDR_BROWSER_TASK") or None):
        raise Fail("Ask from the worker that owns this task.")
    if task_closed(mine["task"]):
        raise Fail("This task has finished.")
    if args.site is not None:
        target = {"kind": "site", "origin": safe_origin(args.site)}
    else:
        target = {"kind": "browser" if args.browser else "terminal"}
    used = browser_marker(mine["task"]) if args.browser else None

    root = ask_dir()
    for part in ("req", "ans", "end"):
        (root / part).mkdir(parents=True, exist_ok=True, mode=0o700)
    # One live request per terminal. Never remove the lock file while another process can hold it.
    with (root / f".login-{address}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Fail("A sign-in Ask is already waiting in this terminal.") from None
        return wait_registered(root, address, mine, target, used)


def label(address, ident):
    try:
        return run(["hc", "label", address, f"herdr.login.ask={ident}"], timeout=4).returncode == 0
    except (Fail, OSError, subprocess.SubprocessError):
        return False


def wait_registered(root, address, mine, target, used):
    at = int(time.time())
    ident = f"{address}.login.{at}.{os.getpid()}"
    req, ans, end = (root / part / (ident + suffix) for part, suffix in
                     (("req", ".json"), ("ans", ".json"), ("end", "")))
    tmp = req.with_suffix(".tmp")
    handlers = {}
    published = False

    def stop(_sig, _frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, stop)
        with os.fdopen(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
            json.dump({"tool_name": TOOL, "tool_input": target, "owner": mine}, f)
        if used:
            used.touch()
        tmp.replace(req)
        published = label(address, ident)
        print("Sign-in Ask posted. Waiting for the person.", flush=True)
        until = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < until:
            if owner(address) != mine or task_closed(mine["task"]):
                end.write_text("The session ended\n")
                return 1
            try:
                reply = json.loads(ans.read_text()).get("login")
            except (ValueError, AttributeError, FileNotFoundError):
                reply = None
            if reply == "confirmed":
                if not end.exists():
                    end.write_text("Sign-in confirmed\n")
                print("The person confirmed. Verify the sign-in in the original session before continuing.")
                return 0
            if reply == "canceled":
                if not end.exists():
                    end.write_text("Sign-in canceled\n")
                print("The person canceled the sign-in Ask.")
                return 1
            if used and used.with_suffix(".task").is_file():
                used.touch()
            time.sleep(2)
        end.write_text("Sign-in Ask expired\n")
        print("The sign-in Ask expired.")
        return 1
    except KeyboardInterrupt:
        end.write_text("Sign-in Ask interrupted\n")
        return 1
    finally:
        for file in (tmp, req, ans):
            file.unlink(missing_ok=True)
        if published and owner(address) == mine:
            label(address, "")
        for sig, handler in handlers.items():
            signal.signal(sig, handler)

"""Bounded Actions log reads and a store of previously verified release evidence."""

import io
import json
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import zipfile
import zlib
from collections import Counter, defaultdict
from contextlib import closing, suppress
from time import monotonic

from . import deploy

# Limit bytes read from both gh and individual ZIP entries. Never extract archive paths to disk.
LOG_BYTES = 4 * 1024 * 1024
JOB_LOG_BYTES = 2 * 1024 * 1024
ZIP_ENTRIES = 2048
PROOF_LIMIT = 512
CHECKOUT = re.compile(rb"\[command\][^\r\n]*git log -1 --format=%H\r?\n[^\S\n]*"
                      rb"(?:\d{4}-\S+Z\s+)?([a-f0-9]{40})(?:\s|$)")


def bounded_output(argv, *, timeout, cap, cwd, env=None):
    """Read bounded process output, terminating its whole group on timeout or overflow."""
    deadline = monotonic() + timeout
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as process:
        with selectors.DefaultSelector() as selector:
            out, err = bytearray(), bytearray()
            selector.register(process.stdout, selectors.EVENT_READ, out)
            selector.register(process.stderr, selectors.EVENT_READ, err)
            try:
                while selector.get_map():
                    left = deadline - monotonic()
                    if left <= 0 or not (events := selector.select(left)):
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in events:
                        part = os.read(key.fd, 65536)
                        if part:
                            key.data.extend(part)
                            if len(out) + len(err) > cap:
                                raise deploy.GitHubError("The evidence download exceeded its size limit.")
                        else:
                            selector.unregister(key.fileobj)
                return process.wait(timeout=max(0, deadline - monotonic())), bytes(out), bytes(err)
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)


def checkout_sha(log):
    shas = set(CHECKOUT.findall(log))
    return shas.pop().decode("ascii") if len(shas) == 1 else None


def archive_checkouts(data, all_jobs, required):
    """Bind a unique whole-job transcript to a job from this exact run attempt."""
    # Actions flattens reusable-workflow job names in the archive. Collisions with any
    # other job (including skipped jobs) require the API endpoint for the numeric job ID.
    def log_names(job):
        name = job["name"].replace(":", "")
        # Both archive formats occur for jobs from reusable workflows.
        return {name.replace("/", "_"), name.replace("/", "")}

    names = Counter(name for job in all_jobs for name in log_names(job))
    # Github has also emitted truncated names. Treat a possible truncation as a collision
    # without accepting a truncated name as evidence by itself.
    for job in all_jobs:
        for name in log_names(job):
            encoded = name.encode("utf-16-le")
            if len(encoded) > 180:
                names[encoded[:180].decode("utf-16-le", errors="ignore")] += 1
    logs = defaultdict(list)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > ZIP_ENTRIES:
                return {}
            for entry in entries:
                match = re.fullmatch(r"\d+_([^/]+)\.txt", entry.filename)
                if match:
                    logs[match[1]].append(entry)
            result = {}
            for job in required:
                matches = [(name, item) for name in log_names(job) for item in logs[name]]
                if len(matches) == 1:
                    name, entry = matches[0]
                    if names[name] != 1 or entry.file_size > JOB_LOG_BYTES:
                        continue
                    with archive.open(entry) as log:
                        # A mapped transcript with ambiguous evidence stays unverified.
                        proof = log.read(JOB_LOG_BYTES + 1)
                        if len(proof) <= JOB_LOG_BYTES:
                            result[job["id"]] = checkout_sha(proof)
            return result
    except (OSError, EOFError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error):
        return {}


class VerifiedCheckouts:
    """Cache only positive proof. Missing, busy or damaged cache state is a cache miss."""

    def __init__(self, path):
        self.path = path

    def connect(self, write=False):
        # Keep private repository evidence in a private file. No raw logs or credentials.
        if write:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.close(os.open(self.path, os.O_CREAT | os.O_NOFOLLOW | os.O_WRONLY | os.O_NONBLOCK, 0o600))
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("Unsafe release evidence cache")
        mode = "rw" if write else "ro"
        return sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", timeout=0.05, uri=True)

    def get(self, key, sha):
        if self.path is None:
            return None
        try:
            with closing(self.connect()) as db:
                row = db.execute("SELECT evidence FROM verified WHERE key = ?", (key,)).fetchone()
            evidence = json.loads(row[0]) if row else None
            if evidence and all(
                    isinstance(job["job"], str) and type(job["id"]) is int and job["checkout_sha"] == sha
                    for job in evidence):
                return evidence
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            pass
        return None

    def put(self, key, evidence):
        if self.path is None:
            return
        try:
            with closing(self.connect(write=True)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS verified (key TEXT UNIQUE, evidence TEXT NOT NULL)")
                db.execute("INSERT OR IGNORE INTO verified VALUES (?, ?)", (key, json.dumps(evidence)))
                db.execute("DELETE FROM verified WHERE rowid IN "
                           "(SELECT rowid FROM verified ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (PROOF_LIMIT,))
        except (OSError, sqlite3.Error):
            pass


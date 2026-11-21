"""Explicit, machine-local dispatch receipts for the Comando main app action."""

import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from . import deploy
from .base import STORE, now
from .main_app_release import (
    ENVIRONMENTS, REF, REPO, SHA, ReleaseGitHub, digest, parallel, release_plan, run_link, run_view, verify_run,
)


def ledger_path():
    return os.path.join(STORE, ".main-app-deploy.sqlite3")


class Ledger:
    """One writer authority for all Comando clicks, including overlapping selections."""

    def __init__(self, path):
        self.path = path

    @contextmanager
    def reader(self):
        try:
            db = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            if not self.path.exists():
                yield None
                return
            raise
        with closing(db):
            yield db

    @contextmanager
    def writer(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, plan TEXT NOT NULL, current TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS receipts (
                request TEXT, environment TEXT, target TEXT, at INTEGER, head TEXT, state TEXT, run INTEGER,
                reason TEXT, PRIMARY KEY (request, environment, target))""")
            db.execute("CREATE INDEX IF NOT EXISTS target_receipts ON receipts (environment, target)")
            yield db

    def request(self, request):
        with self.reader() as db:
            if db is None:
                return None
            row = db.execute("SELECT plan FROM requests WHERE id = ?", (request,)).fetchone()
            return json.loads(row[0]) if row else None

    def receipts(self, request):
        with self.reader() as db:
            if db is None:
                return {}
            rows = db.execute("SELECT environment, target, state, run, reason FROM receipts WHERE request = ?", (request,))
            return {(env, target): (state, run, reason) for env, target, state, run, reason in rows}

    def current(self, request):
        with self.reader() as db:
            if db is None:
                return None
            return json.loads(db.execute("SELECT current FROM requests WHERE id = ?", (request,)).fetchone()[0])

    def reserve(self, plan, current, ready):
        """Commit every reservation before a POST. An interrupted writer leaves a durable hold."""
        request = plan["plan_id"]
        with self.writer() as db:
            db.execute("INSERT OR IGNORE INTO requests VALUES (?, ?, ?)", (request, json.dumps(plan), json.dumps(current)))
            reserved, held = [], {}
            for row in ready:
                env, target = row["environment"], row["target"]
                existing = db.execute("""SELECT request, state, run FROM receipts
                    WHERE environment = ? AND target = ?""", (env, target))
                # A new baseline can close a completed run or a definite rejection. An
                # untracked POST that GitHub may have accepted stays held.
                safe = set(row["clears"])
                unseen = [(other, state, run) for other, state, run in existing
                          if other == request or (state != "not_sent" and other not in safe)]
                if unseen:
                    held[(env, target)] = unseen
                    continue
                db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?, 'reserved', NULL, NULL)",
                           (request, env, target, now(), plan["head"]))
                reserved.append(row)
            for row in rows_of(current):
                key = (row["environment"], row["target"])
                if key in held:
                    row.update(state="held", reason="An earlier request already reserved or sent this target.",
                               prior_requests=[{"id": other, "state": state,
                                                "run": {"id": run, "url": run_link(run)} if run else None}
                                               for other, state, run in held[key]])
            db.execute("UPDATE requests SET current = ? WHERE id = ?", (json.dumps(current), request))
        return reserved

    def finish(self, request, row, state, run=None, reason=None):
        with self.writer() as db:
            db.execute("""UPDATE receipts SET state = ?, run = ?, reason = ?
                WHERE request = ? AND environment = ? AND target = ? AND state = 'reserved'""",
                       (state, run, reason, request, row["environment"], row["target"]))

    def prior_submissions(self, env, target):
        with self.reader() as db:
            if db is None:
                return []
            return db.execute("""SELECT request, head, run FROM receipts
                WHERE environment = ? AND target = ? AND state IN ('submitted', 'rejected')""",
                              (env, target)).fetchall()


def rows_of(plan):
    return [row for env in plan["environments"].values() for row in env["targets"]]


def intent(plan):
    """Which code, PR decisions, dependencies, and workflow definitions were approved."""
    return {
        "head": plan["head"], "sources": plan["sources"],
        "units": [{key: row[key] for key in ("environment", "target", "file", "workflow_digest", "depends_on", "inputs")}
                  for row in rows_of(plan)],
    }


def submit(g, ledger, plan, row):
    request = plan["plan_id"]
    try:
        head = g.head(REF)
    except deploy.GitHubError as error:
        ledger.finish(request, row, "not_sent", reason=str(error))
        return
    if head != plan["head"]:
        ledger.finish(request, row, "not_sent", reason="Master moved before the POST.")
        return
    try:
        run, _ = g.dispatch(row["file"], REF, row["inputs"], api_version="2026-03-10")
    except deploy.GitHubError as error:
        # A failed transport or an unexpected GitHub response cannot rule out acceptance.
        rejected = re.fullmatch(r"HTTP (400|401|403|404|405|410|422)", str(error))
        ledger.finish(request, row, "rejected" if rejected else "ambiguous", reason=str(error))
        return
    if run:
        ledger.finish(request, row, "submitted", run=run)
    else:
        ledger.finish(request, row, "ambiguous", reason="GitHub did not return an exact run ID.")


def receipt_view(g, row, receipt):
    state, run_id, reason = receipt
    if state == "reserved":
        return {"state": "submitting", "reason": "Reserved before the POST. An interrupted submission remains held."}
    if not run_id:
        return {"state": state, "reason": reason}
    try:
        run = g.exact_run(run_id)
    except deploy.GitHubError as error:
        return {"state": "submitted", "reason": str(error), "run": {"id": run_id, "url": run_link(run_id)}}
    result = {"run": run_view(run), "reason": None}
    if (run["id"] != run_id or run.get("event") != "workflow_dispatch" or run.get("head_branch") != REF
            or run.get("path", "").split("@")[0] != f".github/workflows/{row['file']}" or run.get("title") != row["title"]):
        return {**result, "state": "unverified", "reason": "The receipt does not match the expected workflow and inputs."}
    if run["status"] != "completed":
        result["state"] = "submitted" if run["status"] in {"queued", "requested", "pending", "waiting"} else "running"
    elif run["conclusion"] != "success":
        result.update(state="failed", reason=run["conclusion"])
    else:
        evidence, problem = verify_run(g, row["target"], row["environment"], run)
        result["run"]["jobs"] = evidence
        result.update(state="passed" if not problem else "unverified", reason=problem)
        result["checkout_drift"] = any(job["checkout_sha"] and job["checkout_sha"] != row["expected_head"] for job in evidence)
    if run["head_sha"] != row["expected_head"]:
        result["head_drift"] = True
        result["reason"] = "GitHub recorded a different master SHA at dispatch."
        if result["state"] == "passed":
            result["state"] = "unverified"
    return result


def request_view(g, ledger, plan):
    request = plan["plan_id"]
    receipts = ledger.receipts(request)
    current_rows = {(row["environment"], row["target"]): row for row in rows_of(ledger.current(request) or plan)}

    def one(row):
        key = (row["environment"], row["target"])
        if key in receipts:
            result = receipt_view(g, row, receipts[key])
        else:
            result = current_rows[key]
        return {**row, **result}

    rows = parallel(one, rows_of(plan))
    try:
        observed_head = g.head(REF)
    except deploy.GitHubError:
        observed_head = None
    envs = {}
    for env, original in plan["environments"].items():
        targets = [row for row in rows if row["environment"] == env]
        states = {row["state"] for row in targets}
        if states & {"submitting", "submitted", "running"}:
            state = "running"
        elif states & {"failed", "rejected"}:
            state = "partial_failure" if states & {"passed", "covered"} else "failed"
        elif not states or states <= {"passed", "covered"}:
            state = "passed" if "passed" in states else (
                "blocked" if original["unresolved_changes"] else "no_deploy_needed")
        elif states & {"ready", "waiting"}:
            state = "ready" if "ready" in states else "waiting"
        else:
            state = "blocked"
        if observed_head != plan["head"] and state in {"ready", "waiting"}:
            state = "blocked"
        envs[env] = {**original, "state": state, "targets": targets}
    return {"schema": 1, "request_id": request, "repository": REPO, "ref": REF, "head": plan["head"],
            "observed_head": observed_head, "head_drift": observed_head is not None and observed_head != plan["head"],
            "selection": plan["selection"], "environments": envs}


def start_or_resume(g, ledger, selection, head, plan_id, *, resume=False):
    existing = ledger.request(plan_id)
    if existing:
        if not resume:
            return request_view(g, ledger, existing)
        selection = existing["selection"]
        head = existing["head"]
    if g.head(REF) != head:
        if existing:
            return request_view(g, ledger, existing)
        raise deploy.GitHubError("Master moved since the plan was approved.")
    current = release_plan(g, selection)
    if current["head"] != head or (not existing and current["plan_id"] != plan_id):
        raise deploy.GitHubError("The plan changed. Request a new plan.")
    if existing and intent(existing) != intent(current):
        raise deploy.GitHubError("The approved release intent changed. Request a new plan.")
    approved = existing or current
    allowed = {(row["environment"], row["target"]) for row in rows_of(approved) if row["state"] in {"ready", "waiting"}}
    ready = []
    for row in rows_of(current):
        env, target = row["environment"], row["target"]
        if row["state"] != "ready" or (env, target) not in allowed:
            continue
        before = ledger.prior_submissions(env, target)
        base = row["baseline"]["sha"]
        cleared = []
        for request_id, sha, run_id in before:
            try:
                if (sha == base or g.contains(sha, base)) and (
                        run_id is None or g.exact_run(run_id)["status"] == "completed"):
                    cleared.append(request_id)
            except deploy.GitHubError:
                continue
        ready.append({**row, "clears": cleared})
    reserved = ledger.reserve(approved, current, ready)
    parallel(lambda row: submit(g, ledger, approved, row),
             sorted(reserved, key=lambda row: (row["target"], row["environment"])))
    return request_view(g, ledger, approved)


def cmd_release(args):
    g = ReleaseGitHub()
    ledger = Ledger(Path(ledger_path()).absolute())
    try:
        if args.operation == "plan":
            result = release_plan(g, args.environment)
        elif args.operation == "dispatch":
            if args.resume:
                if args.environment or args.head or args.plan_id:
                    raise deploy.GitHubError("Use --resume by itself or supply --environment, --head and --plan-id.")
                if not re.fullmatch(r"[a-f0-9]{64}", args.resume) or not ledger.request(args.resume):
                    raise deploy.GitHubError("Unknown request ID on this machine.")
                result = start_or_resume(g, ledger, None, None, args.resume, resume=True)
            else:
                if not args.environment or not SHA.fullmatch(args.head or "") or not re.fullmatch(r"[a-f0-9]{64}", args.plan_id or ""):
                    raise deploy.GitHubError("Dispatch requires --environment, --head and --plan-id from the approved plan.")
                existing = ledger.request(args.plan_id)
                if existing and (existing["head"] != args.head or existing["selection"] != args.environment):
                    raise deploy.GitHubError("The request ID does not match the approved head and environment.")
                result = start_or_resume(g, ledger, args.environment, args.head, args.plan_id)
        else:
            plan = ledger.request(args.request_id)
            if not plan:
                raise deploy.GitHubError("Unknown request ID on this machine.")
            result = request_view(g, ledger, plan)
    except (deploy.GitHubError, sqlite3.Error, OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, deploy.GitHubError):
            reason = str(error)
        elif isinstance(error, (sqlite3.Error, OSError)):
            reason = "The local dispatch ledger is unavailable."
        else:
            reason = "GitHub returned an unreadable release response."
        print(json.dumps({"schema": 1, "error": reason}))
        return 1
    print(json.dumps(result))
    return 0

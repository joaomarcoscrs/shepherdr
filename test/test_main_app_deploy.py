"""Comando's release CLI against fixture-only GitHub; the tests never call gh."""
import home_guard  # noqa: F401

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from collections import defaultdict
from pathlib import Path
from threading import Barrier, Lock
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mods/tasks/bin"))

from herdr_task import deploy, main_app_deploy as dispatch, main_app_evidence as evidence, main_app_release as release

FLOOR, A, B, C, NEW = (str(i) * 40 for i in range(1, 6))
WORKFLOWS = Path(__file__).parent / "fixtures/roboflow-deploy"
FUNCTION = "function:light-v2-agent"
HOSTING = "hosting"
TTL = "manual:" + release.TTL


class Fixture:
    def __init__(self):
        self.master = C
        self.changes = {
            A: self.source(A, 101, "Hosting", "app/src/shell.js"),
            B: self.source(B, 102, "functions:light-v2-agent", "app/functions/agent.js"),
            C: self.source(C, 103, "Hosting", "app/src/welcome.js"),
        }
        self.runs = {}
        self.files = defaultdict(list)
        self.job_shas = {}
        self.job_rows = {}
        self.posts = []
        self.outcomes = {}
        self.lock = Lock()
        self.verified = evidence.VerifiedCheckouts(None)
        self.post_barrier = None
        self.add("hosting", "staging", A)
        self.add("hosting", "prod", FLOOR)
        self.add(FUNCTION, "staging", FLOOR)
        self.add(FUNCTION, "prod", FLOOR)

    @staticmethod
    def source(sha, pr, section, file, patch=None):
        return {"sha": sha, "complete": True, "files": [{"filename": file, "patch": patch}],
                "pr": {"number": pr, "body": "# Github Actions Required to Deploy\n" + section,
                       "html_url": f"https://github.com/{release.REPO}/pull/{pr}"}}

    def head(self, branch):
        assert branch == "master"
        return self.master

    def history(self, head, limit):
        assert head == self.master
        return FLOOR, list(self.changes)[-limit:]

    def change(self, sha):
        return self.changes[sha]

    def planning(self):
        return contextlib.nullcontext(lambda _: contextlib.nullcontext())

    def landed_changes(self, floor, commits, on_paths):
        return [self.change(sha) for sha in commits]

    def file(self, path, sha):
        if path == deploy.ROBOFLOW["index"]:
            return 'addfn_httpv2("light.v2.agent", willrequire("./agent.js", "app"));'
        if path == deploy.ROBOFLOW["template"]:
            return (WORKFLOWS / "pull_request_template.md").read_text()
        file = path.split("/")[-1]
        if file == release.TTL:
            return "name: Deploy Firestore TTL Policies\n          ref: ${{ github.sha }}"
        if file == release.RULES:
            return "name: Deploy Rules\n          ref: ${{ github.ref_name }}"
        # The legacy workflow fixtures only need inputs and titles.
        protection = "inputs.environment == 'prod' && 'prod' || 'staging'" if file == "deploy-universe.yaml" else "matrix.target_env"
        return (WORKFLOWS / file).read_text() + f"\n    environment: ${{{{ {protection} }}}}\n          ref: ${{{{ github.ref_name }}}}\n"

    def workflow_runs(self, file):
        with self.lock:
            return {"total": len(self.files[file]), "runs": [dict(self.runs[i]) for i in reversed(self.files[file])]}

    def checkout_sha(self, job):
        return self.job_shas[job]

    def checkouts(self, run, jobs, required):
        return [self.checkout_sha(job["id"]) for job in required]

    def evidence(self, read, items):
        return [read(item) for item in items]

    def jobs(self, run, attempt):
        return self.job_rows[run]

    def exact_run(self, run):
        return dict(self.runs[run])

    def contains(self, a, b):
        return a <= b

    def add(self, target, env, sha, *, status="completed", conclusion="success", ref="master", checkout=None):
        key, _, part = target.partition(":")
        if key == "manual":
            file, title = part, f"{release.tag_for(key, env)} {release.MANUAL_TITLES[part]}"
        else:
            unit, issue = release.unit_for(target, env, self.file(".github/workflows/" + deploy.WORKFLOWS[key]["file"], sha))
            assert not issue, issue
            file, title = unit["file"], unit["title"]
        run_id = 900 + len(self.runs)
        time = f"2026-11-21T01:{run_id - 900:02}:00Z"
        run = {"id": run_id, "path": f".github/workflows/{file}", "head_sha": sha, "head_branch": ref, "status": status,
               "conclusion": conclusion, "title": title, "event": "workflow_dispatch", "created": time, "updated": time, "attempt": 1}
        self.runs[run_id] = run
        self.files[file].append(run_id)
        family = part if key == "manual" else key
        names = (["Build and deploy Universe"] if key == "universe" else
                 [f"{release.JOB_NAMES[family]} ({region})" for region in release.regions(key, env)])
        if key == "hosting":
            names.append("Prepare hosting bundle")
        self.job_rows[run_id] = []
        for i, name in enumerate(names):
            job = run_id * 10 + i
            self.job_shas[job] = checkout or sha
            self.job_rows[run_id].append({"id": job, "name": name, "conclusion": "success",
                                         "steps": [{"name": step, "conclusion": "success"}
                                                   for step in release.PUBLISH_STEPS[family]]})
        return run_id

    def add_pr_run(self, status, checkout="feature"):
        run_id = 900 + len(self.runs)
        t = f"2026-11-21T01:{run_id - 900:02}:00Z"
        self.runs[run_id] = {"id": run_id, "path": ".github/workflows/deploy-pr.yaml", "head_sha": NEW,
                             "head_branch": checkout, "status": status, "conclusion": "success",
                             "title": "[Staging + EU Staging] Deploy PR feature", "event": "workflow_dispatch",
                             "created": t, "updated": t, "attempt": 1}
        self.files[release.DEPLOY_PR].append(run_id)
        self.job_rows[run_id] = [{"name": "Hosting / Deploy hosting (staging)", "conclusion": "success"}]
        return run_id

    def dispatch(self, file, ref, inputs, *, api_version):
        assert api_version == "2026-03-10" and ref == "master"
        assert set(inputs).isdisjoint({"force", "skip_eu", "sha", "ref"})
        if self.post_barrier:
            self.post_barrier.wait(5)
        env = inputs["environment"]
        key = next(key for key, spec in deploy.WORKFLOWS.items() if spec["file"] == file)
        part = inputs.get("deploy_target") or (inputs.get("deploy_only") or "").removeprefix("functions:")
        target = release.scope(key, part)
        with self.lock:
            self.posts.append((env, target))
            result = self.outcomes.get((env, target))
            if result == "timeout":
                raise deploy.GitHubError("GitHub request timed out")
            if result == "rejected":
                raise deploy.GitHubError("HTTP 422")
            if result == "no-receipt":
                self.add(target, env, self.master, status="queued", conclusion=None)
                return None, None
            sha = NEW if result == "drift" else self.master
            run = self.add(target, env, sha, status="queued", conclusion=None)
            return run, release.run_link(run)

    def complete(self, run, result="success"):
        self.runs[run].update(status="completed", conclusion=result)

def row(plan, env, target):
    return next(row for row in plan["environments"][env]["targets"] if row["target"] == target)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.g = Fixture()
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.path = Path(root.name) / "ledger.sqlite3"
        self.ledger = dispatch.Ledger(self.path)

    def plan(self, env="both"):
        return release.release_plan(self.g, env)

    def start(self, plan):
        return dispatch.start_or_resume(self.g, self.ledger, plan["selection"], plan["head"], plan["plan_id"])

    def resume(self, plan):
        return dispatch.start_or_resume(self.g, self.ledger, None, None, plan["plan_id"], resume=True)

    def test_multiple_merges_and_distinct_verified_baselines(self):
        plan = self.plan()
        self.assertEqual([src["pr"] for src in plan["sources"]], [101, 102, 103])
        self.assertEqual(row(plan, "staging", HOSTING)["baseline"]["sha"], A)
        self.assertEqual(row(plan, "staging", HOSTING)["pending"], [C])
        self.assertEqual(row(plan, "prod", HOSTING)["baseline"]["sha"], FLOOR)
        self.assertEqual(row(plan, "prod", HOSTING)["pending"], [A, C])
        self.assertEqual(row(plan, "prod", FUNCTION)["pending"], [B])
        self.assertEqual(row(plan, "prod", HOSTING)["checkout"], "moving_master")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.g.posts, [])

    def test_no_deploy_needed_for_docs_with_no_deploy_section_value(self):
        self.g.changes = {C: self.g.source(C, 103, "None", "docs/guide.md")}
        plan = self.plan()
        for env in ("staging", "prod"):
            self.assertEqual(plan["environments"][env]["state"], "no_deploy_needed")
        self.start(plan)
        self.assertEqual(self.g.posts, [])

    def test_wrong_or_missing_plan_blocks_other_unverified_effects(self):
        self.g.changes[B] = self.g.source(B, 102, "None", "app/functions/agent.js")
        self.g.changes[C]["pr"] = None
        plan = self.plan("prod")
        self.assertEqual(plan["sources"][1]["missing_targets"], [FUNCTION])
        self.assertEqual(row(plan, "prod", HOSTING)["state"], "blocked")
        self.start(plan)
        self.assertEqual(self.g.posts, [])

    def test_unverified_checkout_and_branch_pr_overlay(self):
        self.g.add(HOSTING, "prod", A, checkout=B)
        self.g.add_pr_run("completed")
        plan = self.plan()
        self.assertEqual(row(plan, "prod", HOSTING)["state"], "unverified")
        self.assertIn("checkout SHA", row(plan, "prod", HOSTING)["reason"])
        self.assertEqual(row(plan, "staging", HOSTING)["state"], "unverified")
        self.assertEqual(row(plan, "staging", FUNCTION)["state"], "ready")

    def test_numbered_and_documented_manual_rollout(self):
        self.g.changes = {
            A: self.g.source(A, 101, "1. functions:light-v2-agent\n2. TTL\n3. Hosting", "app/functions/agent.js"),
            B: self.g.source(B, 102, "TTL", "app/firestore.ttl.json", "+loginlessExampleDrafts"),
            C: self.g.source(C, 103, "Hosting", "app/src/welcome.js", "+/agent-examples"),
        }
        self.g.add(TTL, "prod", FLOOR)
        plan = self.plan("prod")
        self.assertEqual(row(plan, "prod", TTL)["state"], "manual")
        self.assertEqual(row(plan, "prod", HOSTING)["state"], "waiting")
        self.assertEqual(row(plan, "prod", TTL)["checkout"], "event_sha")
        answer = self.start(plan)
        fn_run = row(answer, "prod", FUNCTION)["run"]["id"]
        self.assertEqual(self.g.posts, [("prod", FUNCTION)])
        self.g.complete(fn_run)
        self.resume(plan)
        self.assertEqual(self.g.posts, [("prod", FUNCTION)])
        self.g.add(TTL, "prod", C)
        self.resume(plan)
        self.assertEqual(self.g.posts, [("prod", FUNCTION), ("prod", HOSTING)])

    def test_unknown_manual_and_unclassified_mapping_fail_closed(self):
        self.g.changes[C] = self.g.source(C, 103, "Storage Rules\nHosting", "app/storage.rules")
        plan = self.plan("prod")
        self.assertEqual(row(plan, "prod", HOSTING)["state"], "waiting")
        self.assertEqual(row(plan, "prod", "manual:deploy-rules.yaml")["state"], "manual")
        self.assertEqual(row(plan, "prod", FUNCTION)["state"], "ready")
        self.g.changes[C] = self.g.source(C, 103, "None\n$(touch /tmp/never-run)", "packages/shared/env.js")
        plan = self.plan("prod")
        self.assertEqual(row(plan, "prod", FUNCTION)["state"], "blocked")
        self.assertIsNone(plan["sources"][-1]["files"]["packages/shared/env.js"])

    def test_completed_manual_prerequisite_in_wrong_order_does_not_unlock_hosting(self):
        self.g.changes[C] = self.g.source(C, 103, "1. functions:light-v2-agent\n2. TTL\n3. Hosting",
                                          "app/src/welcome.js", "+/agent-examples")
        self.g.add(TTL, "prod", C)
        self.g.add(FUNCTION, "prod", C)
        plan = self.plan("prod")
        self.assertEqual(row(plan, "prod", TTL)["state"], "unverified")
        self.assertEqual(row(plan, "prod", HOSTING)["state"], "waiting")
        self.start(plan)
        self.assertEqual(self.g.posts, [])

    def test_plan_change_or_head_drift_refuses_before_post(self):
        plan = self.plan()
        self.g.changes[C]["pr"]["body"] += "\nPublic"
        with self.assertRaisesRegex(deploy.GitHubError, "plan changed"):
            self.start(plan)
        self.g.master = NEW
        with self.assertRaisesRegex(deploy.GitHubError, "Master moved"):
            self.start(plan)
        self.assertEqual(self.g.posts, [])

    def test_run_head_drift_keeps_exact_link_and_does_not_pass(self):
        self.g.outcomes[("prod", HOSTING)] = "drift"
        plan = self.plan("prod")
        answer = self.start(plan)
        run = row(answer, "prod", HOSTING)["run"]["id"]
        self.g.complete(run)
        result = row(dispatch.request_view(self.g, self.ledger, plan), "prod", HOSTING)
        self.assertTrue(result["head_drift"])
        self.assertEqual(result["state"], "unverified")
        self.assertEqual(result["run"]["url"], release.run_link(run))

    def test_parallel_partial_failure_submitted_running_passed_and_dedupe(self):
        self.g.outcomes[("prod", FUNCTION)] = "rejected"
        self.g.post_barrier = Barrier(4)
        plan = self.plan()
        answer = self.start(plan)
        self.assertCountEqual(self.g.posts, [(env, target) for env in ("staging", "prod") for target in (HOSTING, FUNCTION)])
        self.assertEqual(row(answer, "prod", FUNCTION)["state"], "rejected")
        self.assertEqual(row(answer, "staging", HOSTING)["state"], "submitted")
        for env in ("staging", "prod"):
            for target in (HOSTING, FUNCTION):
                result = row(answer, env, target)
                if result.get("run"):
                    self.g.runs[result["run"]["id"]]["status"] = "in_progress"
        running = dispatch.request_view(self.g, self.ledger, plan)
        self.assertEqual(row(running, "prod", HOSTING)["state"], "running")
        self.g.complete(row(running, "prod", HOSTING)["run"]["id"])
        self.g.complete(row(running, "staging", HOSTING)["run"]["id"], "failure")
        self.g.complete(row(running, "staging", FUNCTION)["run"]["id"])
        again = self.start(plan)
        self.assertEqual(len(self.g.posts), 4)
        self.assertEqual(row(again, "staging", HOSTING)["state"], "failed")
        self.assertEqual(row(again, "staging", FUNCTION)["state"], "passed")
        self.assertEqual(again["environments"]["prod"]["state"], "partial_failure")
        self.assertEqual(again["environments"]["staging"]["state"], "partial_failure")

    def test_ambiguous_post_persists_across_new_requests_and_tiers_are_independent(self):
        self.g.outcomes[("staging", HOSTING)] = "timeout"
        stage = self.plan("staging")
        result = self.start(stage)
        self.assertEqual(row(result, "staging", HOSTING)["state"], "ambiguous")
        prod = self.plan("prod")
        result = self.start(prod)
        self.assertEqual(row(result, "prod", HOSTING)["state"], "submitted")
        both = self.plan()
        result = self.start(both)
        self.assertEqual(row(result, "staging", HOSTING)["state"], "held")
        self.assertEqual(row(dispatch.request_view(self.g, self.ledger, both), "staging", HOSTING)["state"], "held")
        self.assertEqual(self.g.posts.count(("staging", HOSTING)), 1)

    def test_missing_run_id_is_held_and_has_no_exact_receipt(self):
        self.g.outcomes[("prod", HOSTING)] = "no-receipt"
        plan = self.plan("prod")
        result = self.start(plan)
        self.assertEqual(row(result, "prod", HOSTING)["state"], "ambiguous")
        self.assertNotIn("run", row(result, "prod", HOSTING))
        self.resume(plan)
        self.assertEqual(self.g.posts.count(("prod", HOSTING)), 1)

    def test_new_changes_can_use_a_verified_baseline_after_manual_recovery(self):
        self.g.outcomes[("prod", HOSTING)] = "rejected"
        prior = self.plan("prod")
        answer = self.start(prior)
        self.assertEqual(row(answer, "prod", HOSTING)["state"], "rejected")
        self.start(prior)
        self.assertEqual(self.g.posts.count(("prod", HOSTING)), 1)
        self.g.add(HOSTING, "prod", C)
        self.g.master = NEW
        self.g.changes[NEW] = self.g.source(NEW, 104, "Hosting", "app/src/new.js")
        self.g.outcomes.pop(("prod", HOSTING))
        answer = self.start(self.plan("prod"))
        self.assertEqual(row(answer, "prod", HOSTING)["state"], "submitted")

    def test_concurrent_database_reservations_are_disjoint(self):
        plan = self.plan()
        stage = self.plan("staging")
        barrier = Barrier(2)

        def reserve(p):
            rows = [{**r, "clears": []} for r in dispatch.rows_of(p)]
            barrier.wait(3)
            return self.ledger.reserve(p, p, rows)

        initial, other = release.parallel(reserve, [plan, stage])
        keys = [(r["environment"], r["target"]) for r in initial + other]
        self.assertCountEqual(keys, [(env, target) for env in ("staging", "prod") for target in (HOSTING, FUNCTION)])

    def test_cli_emits_json_on_plan_dispatch_status_and_refusal(self):
        def cli(operation, **kwargs):
            buf = io.StringIO()
            with mock.patch.object(dispatch, "ReleaseGitHub", return_value=self.g), \
                    mock.patch.object(dispatch, "ledger_path", return_value=self.path), contextlib.redirect_stdout(buf):
                code = dispatch.cmd_release(argparse.Namespace(operation=operation, **kwargs))
            return code, json.loads(buf.getvalue())

        code, plan = cli("plan", environment="both")
        self.assertEqual(code, 0)
        self.g.diagnostics = {"elapsed_ms": 100, "phases": {}}
        code, measured = cli("plan", environment="both", diagnostics=True)
        self.assertEqual(code, 0)
        self.assertEqual(measured.pop("diagnostics"), self.g.diagnostics)
        self.assertEqual(measured, plan)
        code, sent = cli("dispatch", environment="both", head=plan["head"], plan_id=plan["plan_id"], resume=None)
        self.assertEqual(code, 0)
        self.assertEqual(sent["request_id"], plan["plan_id"])
        code, status = cli("status", request_id=sent["request_id"])
        self.assertEqual(code, 0)
        self.assertEqual(status, sent)
        code, missing = cli("status", request_id="0" * 64)
        self.assertEqual(code, 1)
        self.assertIn("error", missing)

    def test_checkout_parser_reads_only_actions_checkout_command(self):
        sample = (f"2026-11-21T10:42:00.01Z [command]/usr/bin/git log -1 --format=%H\n"
                  f"2026-11-21T10:42:00.02Z {A}\nOther text {B}")
        api = release.ReleaseGitHub()
        with mock.patch.object(api, "log", side_effect=[
                value.encode() for value in (sample, sample + f"\n[command]/usr/bin/git log -1 --format=%H\n{B}", B)]):
            self.assertEqual(api.checkout_sha(91), A)
            self.assertIsNone(api.checkout_sha(92))
            self.assertIsNone(api.checkout_sha(93))

    def test_adapter_follows_first_parents_and_reads_all_commit_file_pages(self):
        api = release.ReleaseGitHub()
        parents = lambda sha, *ps: {"sha": sha, "parents": [{"sha": p} for p in ps]}
        with mock.patch.object(api, "api", side_effect=[
            [parents(C, B, NEW), parents(NEW, FLOOR), parents(B, A)],
            parents(A, FLOOR),
            [{**parents(A, FLOOR), "files": [{"filename": name, "changes": 1, "patch": "+modified"}]}
             for name in ("app/src/a.js", "app/src/b.js")],
        ]) as request:
            self.assertEqual(api.history(C, 3), (FLOOR, [A, B, C]))
            observed = api.changed_files(A, FLOOR)
            self.assertTrue(observed["complete"])
            self.assertEqual([entry["filename"] for entry in observed["files"]], ["app/src/a.js", "app/src/b.js"])
            self.assertEqual(request.call_args_list[1].args, (f"git/commits/{A}",))

    def test_rest_delta_cannot_clear_a_gap_with_a_missing_patch_or_wrong_parent(self):
        api = release.ReleaseGitHub()
        file = {"filename": "app/src/welcome.js", "changes": 2, "patch": "+one"}
        page = {"sha": A, "parents": [{"sha": FLOOR}], "files": [file]}
        with mock.patch.object(api, "api", return_value=[page]):
            self.assertFalse(api.changed_files(A, FLOOR)["complete"])
            file["patch"] = "-old\n+new"
            self.assertTrue(api.changed_files(A, FLOOR)["complete"])
            self.assertFalse(api.changed_files(A, B)["complete"])

    def test_associated_pr_batch_verifies_exact_master_unique_and_complete(self):
        api = release.ReleaseGitHub()
        direct = {"number": 101, "mergeCommit": {"oid": A}, "mergedAt": "known",
                  "baseRefName": "master", "baseRepository": {"nameWithOwner": release.REPO},
                  "body": "# Github Actions Required to Deploy\nHosting", "url": "https://github.com/roboflow/roboflow/pull/101"}
        stacked = {**direct, "number": 102, "baseRefName": "integration"}
        fork = {**direct, "number": 103, "baseRepository": {"nameWithOwner": "fork/roboflow"}}
        other = {**direct, "number": 104, "mergeCommit": {"oid": C}}
        both = {**direct, "mergeCommit": {"oid": B}}
        connections = [
            {"pageInfo": {"hasNextPage": False}, "nodes": [stacked, fork, other, direct]},
            {"pageInfo": {"hasNextPage": False}, "nodes": [both, {**both, "number": 105}]},
        ]
        repo = {f"c{i}": {"oid": sha, "associatedPullRequests": conn}
                for i, (sha, conn) in enumerate(zip([A, B], connections))}
        with api.planning() as phase, mock.patch.object(
                api, "json", return_value={"data": {"repository": repo}}) as request:
            with phase("PRs"):
                observed = api.associated_prs([A, B])
            self.assertEqual(observed, {A: {"number": 101, "body": direct["body"], "html_url": direct["url"]}, B: None})
            self.assertIn(A, request.call_args.args[-1])
            self.assertIn(B, request.call_args.args[-1])
            connections[1]["pageInfo"]["hasNextPage"] = True
            with self.assertRaisesRegex(release.PlanningError, "too many associated PRs"):
                with phase("PR page boundary"):
                    api.associated_prs([A, B])
            with self.assertRaises(release.PlanningError):
                api.reads.remaining()

    def test_head_moves_after_reservation_without_a_post(self):
        plan = self.plan("prod")
        submit = dispatch.submit

        def new_head(*args):
            self.g.master = NEW
            submit(*args)

        with mock.patch.object(dispatch, "submit", side_effect=new_head):
            result = self.start(plan)
        self.assertTrue(result["head_drift"])
        self.assertEqual(row(result, "prod", HOSTING)["state"], "not_sent")
        self.assertEqual(self.g.posts, [])

    def test_completion_order_and_active_overlap_are_preserved_without_excess_jobs(self):
        old = self.g.add_pr_run("completed")
        self.g.runs[old].update(created="2026-11-21T00:00:00Z", updated="2026-11-21T01:10:00Z")
        self.g.job_rows[old] = [{"name": "Hosting / publish", "conclusion": "success"}]
        newer = self.g.add_pr_run("completed")
        self.g.job_rows[newer] = [{"name": "Hosting / publish", "conclusion": "skipped"}]
        baseline, observed, why = release.coverage(self.g, HOSTING, "staging", {A, FLOOR})
        self.assertIsNone(baseline)
        self.assertNotIn(observed["id"], (old, newer))
        self.assertIn("completion order", why)
        active = self.g.add_pr_run("in_progress")
        with mock.patch.object(self.g, "jobs", wraps=self.g.jobs) as jobs:
            baseline, observed, why = release.coverage(self.g, HOSTING, "staging", {A, FLOOR})
            self.assertIsNone(baseline)
            self.assertEqual(observed["id"], active)
            self.assertIn("active", why)
            jobs.assert_not_called()
        self.g.runs[active]["title"] += " (dry run)"
        self.g.files["deploy-hosting.yaml"].clear()
        with mock.patch.object(self.g, "jobs", wraps=self.g.jobs) as jobs:
            baseline, _, why = release.coverage(self.g, HOSTING, "staging", {A, FLOOR})
            self.assertIsNone(baseline)
            self.assertIn("No full-tier master baseline", why)
            jobs.assert_not_called()

    def test_concurrent_job_and_checkout_reads_are_shared_and_snapshot_is_refreshed(self):
        api = release.ReleaseGitHub()
        responses = {"jobs": [{"jobs": [{"name": "fixture"}]}]}
        gate = Barrier(8)

        def get(path, *args):
            time.sleep(0.01)
            return responses["jobs"] if path.split("?")[0].endswith("/jobs") else {"runs": []}

        def read(_):
            gate.wait(timeout=2)
            return api.workflow_runs("deploy-pr.yaml"), api.jobs(900, 1), api.checkout_sha(9)

        with (mock.patch.object(api, "api", side_effect=get) as request,
              mock.patch.object(api, "read_checkout_sha", return_value=A) as log):
            with api.planning() as phase, phase("coverage"):
                results = release.parallel(read, range(8))
            self.assertEqual(request.call_count, 2)
            log.assert_called_once_with(9)
            self.assertEqual(results, [({"runs": []}, [{"name": "fixture"}], A)] * 8)
            responses["jobs"] = [{"jobs": [{"name": "next snapshot"}]}]
            with api.planning() as phase, phase("coverage"):
                self.assertEqual(api.jobs(900, 1), [{"name": "next snapshot"}])
                api.jobs(900, 2)
            self.assertEqual(request.call_count, 4)

    def test_timeout_and_permissions_report_phase_before_any_reservation_or_post(self):
        for error, message in [
                (subprocess.TimeoutExpired("gh", 0.1), "did not respond in time"),
                ((False, "", "HTTP 403 some private detail"), "read access"),
                ((False, "", "HTTP 403 secondary rate limit"), "rate limited")]:
            with self.subTest(message=message):
                api = release.ReleaseGitHub()
                expected = self.plan()
                with (mock.patch.object(release, "gh", side_effect=[error]),
                      mock.patch.object(api, "dispatch") as post,
                      mock.patch.object(self.ledger, "reserve") as reserve):
                    with self.assertRaisesRegex(release.PlanningError, f"master head.*{message}"):
                        dispatch.start_or_resume(api, self.ledger, "both", C, expected["plan_id"])
                    self.assertEqual(api.diagnostics["phases"]["master head"]["reads"]["github: commit metadata"]["errors"], 1)
                    post.assert_not_called()
                    reserve.assert_not_called()

    def test_local_git_reads_exact_first_parent_trees_without_using_worktree_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()

            def git(*args):
                return subprocess.check_output(["git", "-C", root, *args], text=True, stderr=subprocess.PIPE).strip()

            git("init", "-b", "master")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "user.name", "Fixture")
            git("remote", "add", "origin", "git@github.com:roboflow/roboflow.git")
            (root / "app" / "src").mkdir(parents=True)
            page = root / "app" / "src" / "old.js"
            page.write_text("const a = 1;\n")
            git("add", ".")
            git("commit", "-m", "Root")
            initial = git("rev-parse", "HEAD")
            git("switch", "-c", "fixture-side")
            git("mv", "app/src/old.js", "app/src/new.js")
            marker = root / "app" / "src" / "marker.js"
            marker.write_text('const path = "/agent-examples";\n')
            git("add", ".")
            git("commit", "-m", "Change hosting")
            git("switch", "master")
            git("merge", "--no-ff", "--no-edit", "fixture-side")
            head = git("rev-parse", "HEAD")
            marker.write_text("modified only in worktree")
            status_before = git("status", "--porcelain")
            reads = release.PlanReads()
            local = release.LocalReleaseGit(reads)
            local.root = root
            with reads.phase("exact Git objects"):
                self.assertEqual(local.history(head, 1), (initial, [head]))
                self.assertIsNone(local.history(NEW, 1))
                self.assertEqual(local.file("app/src/marker.js", head), 'const path = "/agent-examples";')
                change = local.change(head, initial)
                self.assertCountEqual(change["files"], [
                    {"filename": "app/src/marker.js"},
                    {"filename": "app/src/new.js", "previous_filename": "app/src/old.js"},
                ])
                self.assertTrue(change["rollout"])
                git("config", "remote.origin.promisor", "true")
                self.assertIsNone(local.history(head, 1))
                git("config", "--unset", "remote.origin.promisor")
                git("remote", "set-url", "origin", "https://github.com/fork/roboflow.git")
                self.assertIsNone(local.history(head, 1))
            self.assertEqual(git("status", "--porcelain"), status_before)

    def test_missing_master_fetches_only_the_exact_ref_into_a_disposable_bare_cache(self):
        root = self.path.parent.resolve()
        source, checkout = root / "source", root / "checkout"
        source.mkdir()

        def git(path, *args):
            return subprocess.check_output(["git", "-C", path, *args], text=True, stderr=subprocess.PIPE).strip()

        git(source, "init", "-b", "master")
        git(source, "config", "user.email", "fixture@example.invalid")
        git(source, "config", "user.name", "Fixture")
        git(source, "commit", "--allow-empty", "-m", "Root")
        seed = git(source, "rev-parse", "HEAD")
        git(root, "clone", "--no-hardlinks", source, checkout)
        git(checkout, "remote", "set-url", "origin", "git@github.com:roboflow/roboflow.git")
        (source / "page").write_text("new master file")
        git(source, "add", ".")
        git(source, "commit", "-m", "Change")
        head = git(source, "rev-parse", "HEAD")
        index = (checkout / ".git/index").stat().st_mtime_ns
        reads = release.PlanReads()
        local = release.LocalReleaseGit(reads)
        local.root = checkout

        def fetch(command, **kwargs):
            self.assertNotEqual(kwargs["cwd"], checkout)
            self.assertEqual(command[-2:], [f"https://github.com/{release.REPO}.git",
                                            "+refs/heads/master:refs/heads/master"])
            self.assertIn("--no-tags", command)
            command = ["protocol.file.allow=always" if arg == "protocol.https.allow=always" else arg
                       for arg in command]
            command[-2] = str(source)
            return evidence.bounded_output(command, **kwargs)

        with contextlib.ExitStack() as scratch, reads.phase("history"), mock.patch.object(
                release, "bounded_output", side_effect=fetch):
            self.assertEqual(local.history(head, 1, scratch=scratch), (seed, [head]))
            cache = local.cache
            self.assertEqual(local.file("page", head), "new master file")
            self.assertEqual(local.change(head, seed)["files"], [{"filename": "page"}])
        self.assertFalse(cache.exists())
        self.assertEqual(git(checkout, "rev-parse", "HEAD", "refs/remotes/origin/master"), f"{seed}\n{seed}")
        self.assertEqual((checkout / ".git/index").stat().st_mtime_ns, index)
        with self.assertRaises(subprocess.CalledProcessError):
            git(checkout, "cat-file", "-t", head)

    def test_attempt_archive_maps_unique_job_names_and_rejects_ambiguous_logs(self):
        first = {"id": 1, "name": "Fast lane (functions:light-v2-agent) / Build once"}
        second = {"id": 2, "name": "Deploy hosting (prod)"}
        log = f"[command]/usr/bin/git log -1 --format=%H\n{A}\n"

        def archive(entries):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zipped:
                for name, text in entries:
                    zipped.writestr(name, text)
            return buf.getvalue()

        name = "Fast lane (functionslight-v2-agent) _ Build once"
        archive_entries = [(f"7_{name}.txt", log), ("0_Deploy hosting (prod).txt", log + log.replace(A, B))]
        self.assertEqual(evidence.archive_checkouts(archive(archive_entries), [first, second], [first, second]),
                         {1: A, 2: None})
        collision = {"id": 3, "name": name}
        self.assertNotIn(1, evidence.archive_checkouts(archive(archive_entries), [first, second, collision], [first]))
        self.assertNotIn(1, evidence.archive_checkouts(archive(archive_entries + [(f"8_{name}.txt", log)]),
                                                       [first, second], [first]))
        with mock.patch.object(evidence, "JOB_LOG_BYTES", 1):
            self.assertEqual(evidence.archive_checkouts(archive(archive_entries), [first, second], [first, second]), {})

    def test_archive_and_job_endpoints_use_exact_attempt_and_numeric_job_id(self):
        api = release.ReleaseGitHub()
        run = {"id": 50, "attempt": 2}
        jobs = [{"id": 51, "name": "One"}, {"id": 52, "name": "Two"}]
        log = f"[command]git log -1 --format=%H\n{A}\n".encode()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zipped:
            zipped.writestr("1_One.txt", log)
        with mock.patch.object(api, "log", side_effect=[buf.getvalue(), log]) as read:
            self.assertEqual(api.checkouts(run, jobs, jobs), [A, A])
            self.assertEqual([call.args[0] for call in read.call_args_list],
                             ["actions/runs/50/attempts/2/logs", "actions/jobs/52/logs"])

    def test_cache_skips_verified_jobs_but_rechecks_run_status_prs_and_master(self):
        path = self.path.with_name("release-evidence.sqlite3")
        self.g.verified = evidence.VerifiedCheckouts(path)
        initial = self.plan()
        self.g.verified = evidence.VerifiedCheckouts(path)
        with mock.patch.object(self.g, "jobs", wraps=self.g.jobs) as jobs:
            self.assertEqual(self.plan(), initial)
            jobs.assert_not_called()
            self.g.changes[C]["pr"]["body"] += "\n## Infrastructure impact\nNew rollout needs review."
            self.assertNotEqual(self.plan()["plan_id"], initial["plan_id"])
            run_id = row(initial, "prod", HOSTING)["baseline"]["id"]
            self.g.runs[run_id]["attempt"] = 2
            result = row(self.plan(), "prod", HOSTING)
            self.assertEqual(result["state"], "unverified")
            self.assertIn("rerun", result["reason"])
            self.g.master = NEW
            with self.assertRaisesRegex(deploy.GitHubError, "Master moved"):
                self.start(initial)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.g.posts, [])

    def test_unproven_jobs_are_retried_and_never_dispatched(self):
        self.g.verified = evidence.VerifiedCheckouts(self.path.with_name("release-evidence.sqlite3"))
        real = self.g.checkouts

        def denied(run, jobs, required):
            if run["path"].endswith("deploy-hosting.yaml"):
                raise deploy.GitHubError("HTTP 403")
            return real(run, jobs, required)

        with mock.patch.object(self.g, "checkouts", side_effect=denied):
            plan = self.plan("prod")
            self.assertEqual(row(plan, "prod", HOSTING)["state"], "unverified")
            self.assertIn("HTTP 403", row(plan, "prod", HOSTING)["reason"])
            self.assertEqual(row(plan, "prod", FUNCTION)["state"], "ready")
            self.start(plan)
        self.assertNotIn(("prod", HOSTING), self.g.posts)
        self.assertEqual(row(self.plan("prod"), "prod", HOSTING)["state"], "ready")

    def test_log_read_enforces_real_process_time_and_byte_limits(self):
        api = release.ReleaseGitHub()
        runner = self.path.parent / "fixture-gh"
        runner.write_text(f"#!{sys.executable}\nimport sys, time\n"
                          "if 'slow' in sys.argv[2]: time.sleep(10)\n"
                          "else: sys.stdout.write('x' * 4096)\n")
        runner.chmod(0o700)
        with mock.patch.object(release, "GH", str(runner)):
            start = time.monotonic()
            with self.assertRaisesRegex(deploy.GitHubError, "did not respond in time"):
                api.log("slow", 1024, "logs", seconds=0.25)
            self.assertLess(time.monotonic() - start, 2)
            with self.assertRaisesRegex(deploy.GitHubError, "size limit"):
                api.log("large", 1024, "logs")

    def test_cli_preserves_scoped_evidence_if_final_master_read_fails(self):
        out = io.StringIO()
        with (mock.patch.object(dispatch, "ReleaseGitHub", return_value=self.g),
              mock.patch.object(dispatch, "ledger_path", return_value=self.path),
              mock.patch.object(self.g, "head", side_effect=[C, deploy.GitHubError("GitHub timed out")]),
              contextlib.redirect_stdout(out)):
            code = dispatch.cmd_release(argparse.Namespace(operation="plan", environment="both", diagnostics=False))
        result = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertNotIn("plan_id", result)
        self.assertEqual(result["partial"]["scan"]["through"], C)
        self.assertEqual(len(result["partial"]["sources"]), 3)
        hosted = row(result["partial"], "prod", HOSTING)
        self.assertEqual(hosted["state"], "blocked")
        self.assertEqual(hosted["baseline"]["sha"], FLOOR)
        self.assertEqual(self.g.posts, [])
        self.assertFalse(self.path.exists())

    def test_read_budget_produces_schema_one_error_without_plan_or_ledger(self):
        api = release.ReleaseGitHub()
        out = io.StringIO()
        with (mock.patch.object(release, "READ_LIMIT", 1),
              mock.patch.object(release.LocalReleaseGit, "history", return_value=None),
              mock.patch.object(release, "gh", return_value=(True, C, "")) as request,
              mock.patch.object(dispatch, "ReleaseGitHub", return_value=api),
              mock.patch.object(dispatch, "ledger_path", return_value=self.path),
              contextlib.redirect_stdout(out)):
            code = dispatch.cmd_release(argparse.Namespace(operation="plan", environment="both", diagnostics=True))
        result = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["schema"], 1)
        self.assertIn("first-parent history", result["error"])
        self.assertIn("read limit", result["error"])
        self.assertIn("master head", result["diagnostics"]["phases"])
        self.assertNotIn("plan_id", result)
        self.assertFalse(Path(self.path).exists())
        request.assert_called_once()

    def test_interrupted_reservation_holds_target_across_selections(self):
        stage = self.plan("staging")
        hosted = row(stage, "staging", HOSTING)
        self.ledger.reserve(stage, stage, [{**hosted, "clears": []}])
        self.assertEqual(row(dispatch.request_view(self.g, self.ledger, stage), "staging", HOSTING)["state"], "submitting")
        both = self.plan()
        result = self.start(both)
        self.assertEqual(row(result, "staging", HOSTING)["state"], "held")
        self.assertEqual(row(result, "prod", HOSTING)["state"], "submitted")
        self.assertNotIn(("staging", HOSTING), self.g.posts)


if __name__ == "__main__":
    unittest.main()

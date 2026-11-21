"""Comando's release CLI against fixture-only GitHub; the tests never call gh."""
import home_guard  # noqa: F401

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from threading import Barrier, Lock
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mods/tasks/bin"))

from herdr_task import deploy, main_app_deploy as dispatch, main_app_release as release

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
        with mock.patch.object(api, "call", side_effect=[sample, sample + f"\n[command]/usr/bin/git log -1 --format=%H\n{B}", B]):
            self.assertEqual(api.checkout_sha(91), A)
            self.assertIsNone(api.checkout_sha(92))
            self.assertIsNone(api.checkout_sha(93))

    def test_adapter_follows_first_parents_and_reads_all_commit_file_pages(self):
        api = release.ReleaseGitHub()
        parents = lambda sha, *ps: {"sha": sha, "parents": [{"sha": p} for p in ps]}
        direct = {"number": 101, "merge_commit_sha": A, "merged_at": "known", "base": {"ref": "master"}}
        stacked = {**direct, "number": 102, "base": {"ref": "integration"}}
        other = {**direct, "number": 103, "merge_commit_sha": C}
        with mock.patch.object(api, "api", side_effect=[
            [parents(C, B, NEW), parents(NEW, FLOOR), parents(B, A)],
            parents(A, FLOOR),
            [{"files": [{"filename": "app/src/a.js"}]}, {"files": [{"filename": "app/src/b.js"}]}],
            [[other], [stacked, direct]],
        ]):
            self.assertEqual(api.history(C, 3), (FLOOR, [A, B, C]))
            observed = api.change(A)
            self.assertEqual(observed["pr"], direct)
            self.assertEqual([entry["filename"] for entry in observed["files"]], ["app/src/a.js", "app/src/b.js"])

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

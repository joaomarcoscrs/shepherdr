"""Read-only release analysis for the Comando main app action."""

import hashlib
import json
import posixpath
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from subprocess import TimeoutExpired

from . import deploy
from .github import gh

REPO = deploy.ROBOFLOW["repo"]
REF = deploy.ROBOFLOW["branch"]
ENVIRONMENTS = {"staging": ("staging",), "prod": ("prod",), "both": ("staging", "prod")}
SCAN_LIMIT = 32
SHA = re.compile(r"[0-9a-f]{40}")
TTL = "deploy-ttl.yaml"
INDEXES = "deploy-indexes.yaml"
RULES = "deploy-rules.yaml"
NOTICE = "frontend-refresh-notice.yaml"
FAST = "deploy-function-fast-lane.yaml"
DEPLOY_PR = "deploy-pr.yaml"
RAPID = "deploy-rapid.yaml"
JOB_NAMES = {
    "function": "Build and deploy function",
    "hosting": "Deploy hosting",
    "api": "Build and Deploy API",
    "admin": "Build and deploy admin",
    "public": "Deploy Public",
    "rapid": "Build and Deploy Rapid",
    TTL: "Deploy Firestore TTL Policies",
    INDEXES: "Build and Deploy Firestore Indexes",
}
PUBLISH_STEPS = {
    "function": ("Deploy Cloud Function",),
    "hosting": ("Deploy Hosting",),
    "api": ("Deploy API hosting and functions",),
    "admin": ("Deploy Admin hosting and functions",),
    "public": ("Deploy Public hosting",),
    "rapid": ("Deploy Rapid hosting", "Deploy Rapid Functions"),
    "universe": ("Deploy Universe hosting and functions",),
    TTL: ("Deploy TTL Policies",),
    INDEXES: ("Deploy Indexes",),
}
MANUAL_TITLES = {TTL: "Deploy TTL Policies", INDEXES: "Deploy Indexes"}
RUN_FIELDS = (
    "{id, title: .display_title, head_sha, head_branch, status, conclusion, created: .created_at, "
    "updated: .updated_at, event, path, attempt: .run_attempt}"
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parallel(fn, seq):
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(fn, seq))


class ReleaseGitHub(deploy.GitHub):
    def __init__(self):
        super().__init__(REPO)

    def call(self, *args):
        try:
            ok, out, error = gh(*args, "--hostname", "github.com", cwd="/", timeout=30)
        except TimeoutExpired:
            raise deploy.GitHubError("GitHub request timed out") from None
        if not ok:
            # Preserve a status code for diagnosis without returning arbitrary response or log text.
            status = re.search(r"\bHTTP [1-5][0-9]{2}\b", error)
            raise deploy.GitHubError(status.group() if status else "GitHub request failed")
        return out

    def api(self, path, *args):
        return self.json("api", f"repos/{REPO}/{path}", *args)

    def history(self, head, limit):
        # REST lists the graph, so follow only explicit first-parent edges, fetching parents
        # omitted from the page (for example after a merge with a large side branch).
        page = self.api(f"commits?sha={head}&per_page=100")
        commits = {row["sha"]: row for row in page}
        landed = []
        while head and len(landed) < limit:
            row = commits.get(head) or self.api(f"commits/{head}")
            parents = row["parents"]
            parent = parents[0]["sha"] if parents else None
            landed.append(head)
            head = parent
        return head, list(reversed(landed))

    def change(self, sha):
        # GET a merge commit's diff against its first parent. GitHub caps returned files at 3000.
        pages = self.api(f"commits/{sha}?per_page=100", "--paginate", "--slurp")
        files = [row for page in pages for row in page["files"]]
        pulls = self.api(f"commits/{sha}/pulls?per_page=100", "--paginate", "--slurp")
        exact = [pr for page in pulls for pr in page if pr["merge_commit_sha"] == sha
                 and pr.get("merged_at") and pr["base"]["ref"] == REF]
        pr = exact[0] if len(exact) == 1 else None
        return {"sha": sha, "files": files, "pr": pr, "complete": bool(pages) and len(files) < 3000}

    @lru_cache(maxsize=64)
    def workflow_runs(self, file):
        # Across branches and events: staging PR deployments and side workflows can overlap.
        return self.api(f"actions/workflows/{file}/runs?per_page=100", "--jq",
                        f'{{total: .total_count, runs: [.workflow_runs[] | {RUN_FIELDS}]}}')

    def exact_run(self, run_id):
        return self.api(f"actions/runs/{int(run_id)}", "--jq", RUN_FIELDS)

    @lru_cache(maxsize=256)
    def jobs(self, run_id, attempt):
        pages = self.api(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", "--paginate", "--slurp")
        return [job for page in pages for job in page["jobs"]]

    @lru_cache(maxsize=256)
    def checkout_sha(self, job_id):
        log = self.call("api", f"repos/{REPO}/actions/jobs/{job_id}/logs")
        # actions/checkout emits this as the last command of the checkout step. Multiple distinct
        # checkouts in one job require separate provenance and aren't accepted here.
        got = re.findall(r"\[command\][^\r\n]*git log -1 --format=%H\r?\n[^\S\n]*"
                         r"(?:\d{4}-\S+Z\s+)?([a-f0-9]{40})(?:\s|$)", log)
        return got[0] if got and len(set(got)) == 1 else None


def scope(workflow, part=None):
    return f"{workflow}:{part}" if part else workflow


def target_for_function(name):
    key = deploy.fn_key(name)
    if key in deploy.API_FUNCTIONS:
        return "api:functions"
    if key in deploy.ADMIN_FUNCTIONS:
        return "admin"
    if key == "rapid-v2-server":
        return "rapid:functions"
    if key == "universe-v2-server":
        return "universe"
    return scope("function", name)


def registrations(text):
    """Direct entry paths from literal registrations. Transitive ownership needs PR evidence."""
    entries = {}
    chunks = re.split(r"\baddfn_\w+\(\s*[\"']([A-Za-z0-9.\-]+)[\"']", text)
    for name, code in zip(chunks[1::2], chunks[2::2]):
        for module in re.findall(r"\bwillrequire\w*\(\s*[\"'](\./[^'\"\n]+)[\"']", code):
            path = posixpath.normpath(posixpath.join("app/functions", module))
            entries.setdefault(path, set()).add(target_for_function(name.replace(".", "-")))
    return entries


def path_scope(path, entries, patch):
    """Known deployed paths. A bare functions path is a transitive ownership question."""
    if (path.startswith(("docs/", "notes/", "openspec/", "e2e/", "tests/", "test/", "spec/", "specs/",
                         ".github/tests/", ".github/ISSUE_TEMPLATE/"))
            or re.search(r"(^|/)(tests?|__tests__|__snapshots__)/|\.(test|spec)\.[cm]?[jt]sx?$", path)
            or path.endswith((".md", ".mdx"))):
        return set()
    if path in entries:
        return entries[path]
    if path == "app/firebase.json":
        changes = [line[1:] for line in patch.splitlines() if line[:1] in {"+", "-"}]
        if changes and all('/query/solutions/loginless-example/' in line and '"light-v2-agent"' in line for line in changes):
            return {"hosting"}
    if path in {"app/functions/index.js", "app/functions/package.json"}:
        return None
    if path.startswith("app/functions/"):
        return {"@backend"}
    if path == "app/firestore.ttl.json":
        return {scope("manual", TTL)}
    if path == "app/firestore.indexes.json":
        return {scope("manual", INDEXES)}
    if path == "app/firestore.rules":
        return {scope("manual", RULES)}
    if path.startswith(("app/storage", "app/database.rules")) and ".rules" in path:
        return {scope("manual", RULES)}
    if path.startswith("rapid/public/src/"):
        # Core App's webpack also builds Rapid source for its embedded view.
        return {"rapid:hosting", "hosting"}
    for prefix, target in (
        ("app/src/", "hosting"), ("app/build/", "hosting"), ("app/webpack.config.js", "hosting"),
        ("admin/", "admin"), ("universe/", "universe"), ("rapid/public/", "rapid:hosting"),
        ("api/functions/", "api:functions"), ("api/", "api:hosting"), ("data/", "public"),
        ("embed/", "manual:deploy-embed.yaml"), ("demo/", "manual:deploy-demo.yaml"),
        ("referrals/", "manual:deploy-referrals.yaml"), ("agent-redirects/", "manual:deploy-agent-redirects.yaml"),
        ("spanner/", "manual:spanner-migrate.yaml"),
    ):
        if path.startswith(prefix):
            return {target}
    return None


def parse_source(change, functions, entries, template):
    pr = change["pr"]
    body = re.sub(r"<!--[\s\S]*?-->", "", (pr or {}).get("body") or "")
    body = re.sub(r"^\s*```[^\n]*\n[\s\S]*?^\s*```\s*$", "", body, flags=re.M)
    headings = re.findall(r"^#{1,3}\s+.*required to deploy[^\n]*", body, re.I | re.M)
    section = deploy.deploy_section(body) if len(headings) == 1 else None
    parsed = deploy.plan(section, functions, template)
    declared = {scope(key, part) for key, part, ok in parsed["items"] if ok}
    errors = []
    owners = {}
    patches = []
    for row in change["files"]:
        path = row["filename"]
        patch = row.get("patch") or ""
        patches.append(patch)
        for name in (path, row.get("previous_filename")):
            if name:
                owners[name] = path_scope(name, entries, patch)
    # A backend PR can declare consumers of a shared backend module. Its rewritten paths still
    # need a deploy decision and direct registrations must agree with that decision.
    backend = {key for key in declared if key.startswith("function:") or key in
               {"api:functions", "rapid:functions", "admin", "universe"}}
    for path, keys in owners.items():
        if keys == {"@backend"} and backend:
            owners[path] = backend
        elif keys == {"@backend"}:
            owners[path] = None
    known = set().union(*(keys or set() for keys in owners.values()))
    unknown = sorted(path for path, keys in owners.items() if keys is None)
    missing = sorted(known - declared)
    if not change["complete"]:
        errors.append("The commit file list is incomplete.")
    if not pr:
        errors.append("No unique merged master PR and deploy section were verified for this commit.")
    elif parsed["state"] == "unavailable" or parsed["unclear"] or any(not ok for _, _, ok in parsed["items"]):
        errors.append(parsed["why"] or "The PR deploy section is incomplete.")
    infrastructure = re.search(r"^#{1,3}[^\S\n]+Infrastructure impact[^\n]*\n([\s\S]*?)(?=^#{1,3}[^\S\n]+|\Z)", body, re.I | re.M)
    if infrastructure:
        impact = [deploy.words(line).lower() for line in infrastructure[1].splitlines() if line.strip()]
        if any(line not in deploy.NO_DEPLOY | {"no", "no infrastructure impact"} for line in impact):
            errors.append("The PR describes infrastructure impact that needs an operator's rollout review.")
    if unknown:
        errors.append("Deployment ownership is unverified for one or more changed paths.")
    if missing:
        errors.append("The PR deploy section omits targets identified by the changed files.")

    # Numbered deploy lines are ordered groups. Unrecognized prose remains a blocker above.
    groups = []
    if section and not errors:
        lines = [line for line in section.splitlines() if line.strip() and " ".join(line.split()) not in template]
        if lines and all(re.match(r"^\s*\d+[.)]\s+", line) for line in lines):
            groups = [[scope(key, part) for key, part, ok in deploy.plan(line, functions)["items"]
                       if ok and scope(key, part) in declared] for line in lines]
    deps = set()
    for i, group in enumerate(groups):
        for after in group:
            deps.update((before, after) for prior in groups[:i] for before in prior if before != after)
    manual = {key for key in declared | known if key.startswith("manual:")}
    rollout = any(re.search(r"^[+-].*(loginlessExampleDrafts|/query/solutions/loginless-example/|/agent-examples)",
                            patch, re.M) for patch in patches)
    if rollout:
        deps.update({("function:light-v2-agent", scope("manual", TTL)), (scope("manual", TTL), "hosting")})
    if not groups:
        # Flat PR lists with manual work have no safe order. The known loginless chain has its own order.
        for before in manual:
            if rollout and before == scope("manual", TTL):
                continue
            deps.update((before, after) for after in declared | known if after not in manual)
    global_gap = bool(unknown or not change["complete"] or parsed["unclear"] or (errors and not (declared | known)))
    if infrastructure and any("infrastructure impact" in issue for issue in errors):
        global_gap = True
    return {"commit": change["sha"], "pr": pr["number"] if pr else None,
            "url": pr["html_url"] if pr else f"https://github.com/{REPO}/commit/{change['sha']}",
            "files": {path: sorted(keys) if keys is not None else None for path, keys in sorted(owners.items())},
            "targets": sorted(declared | known), "missing_targets": missing, "issues": errors, "global_gap": global_gap,
            "manual_edges": sorted([list(edge) for edge in deps]),
            "section_digest": digest(section), "impact_digest": digest(infrastructure[1]) if infrastructure else None}


def regions(key, env):
    return (env,) if key == "universe" else (env, f"eu-{env}")


def target_regions(target, env):
    key, _, part = target.partition(":")
    if key != "manual":
        return list(regions(key, env))
    if part in {TTL, INDEXES, RULES, "deploy-agent-redirects.yaml"}:
        return list(regions(key, env))
    if part in {"deploy-demo.yaml", "deploy-embed.yaml", "deploy-referrals.yaml", "deploy-gsutil-cors.yaml"}:
        return [env]
    return None


def tag_for(key, env):
    if env == "prod":
        return "[Prod]" if key == "universe" else deploy.PROD
    return "[Staging]" if key == "universe" else "[Staging + EU Staging]"


def unit_for(target, env, text):
    key, _, part = target.partition(":")
    if key == "manual":
        return {"file": part, "workflow_url": deploy.workflow_url(REPO, part), "inputs": None,
                "checkout": checkout_mode(text), "workflow_digest": digest(text)}, None
    wf = deploy.WORKFLOWS[key]
    legacy_unit = deploy.units({"repo": REPO, "targets": [
        {"workflow": key, "key": target, "part": part or None, "ref": REF, "status": "ready"}]})[0]
    inputs = {name: value for name, value in legacy_unit["inputs"].items() if name not in {"force", "skip_eu"}}
    inputs["environment"] = env
    problem = deploy.check_workflow(key, text)
    schemas = deploy.dispatch_inputs(text) or {}
    for name, value in inputs.items():
        expected = {"environment": "choice", "deploy_target": "choice", "deploy_only": "string",
                    "deploy_core_app_hosting": "boolean"}[name]
        schema = schemas.get(name) or {}
        actual = schema.get("type") or ("string" if name == "deploy_only" and name in schemas else None)
        if actual != expected or (expected == "choice" and value not in schema["options"]):
            problem = f"{wf['file']}: {name} is unverified for this dispatch."
    for name, spec in schemas.items():
        if name not in inputs and ((spec["required"] and spec["default"] is None)
                or (name in {"force", "skip_eu"} and
                    (spec["type"] != "boolean" or str(spec["default"]).lower() not in {"none", "false"}))):
            problem = f"{wf['file']}: default or required input {name} changed."
    if f"'{tag_for(key, env)}'" not in deploy.run_name(text):
        problem = f"{wf['file']}: the environment's run title is unverified."
    mode = checkout_mode(text)
    if mode == "unknown":
        problem = f"{wf['file']}: checkout semantics are unverified."
    protection = "inputs.environment == 'prod' && 'prod' || 'staging'" if key == "universe" else "matrix.target_env"
    if not re.search(r"^    environment:\s*\$\{\{\s*" + re.escape(protection) + r"\s*\}\}\s*$", text, re.M):
        problem = f"{wf['file']}: the publishing environment binding is unverified."
    return {"file": wf["file"], "workflow_url": legacy_unit["url"], "inputs": inputs,
            "checkout": mode, "workflow_digest": digest(text),
            "title": legacy_unit["title"].replace(wf["tag"], tag_for(key, env), 1)}, problem


def checkout_mode(text):
    refs = set(re.findall(r"^\s+ref:\s*\$\{\{\s*github\.(ref_name|sha)\s*\}\}", text, re.M))
    if "ref_name" in refs:
        return "moving_master"
    return "event_sha" if refs == {"sha"} else "unknown"


def run_link(run_id):
    return f"https://github.com/{REPO}/actions/runs/{int(run_id)}"


def related_files(target):
    key, _, part = target.partition(":")
    if key == "manual":
        return [part]
    others = {"function": [FAST], "hosting": [NOTICE, RAPID], "admin": [FAST]}
    return [deploy.WORKFLOWS[key]["file"], *others.get(key, []), DEPLOY_PR]


def title_covers(target, env, file, title, *, full=True):
    """Run titles identify candidate inputs; job logs must still prove coverage."""
    key, _, part = target.partition(":")
    tier = "Staging" if env == "staging" else "Prod"
    regional = tag_for(key, env)
    match = re.match(r"^(\[[^\]]+\]) (.+)$", title or "")
    if not match:
        return False
    tag, rest = match.groups()
    tags = {regional} if full else {regional, f"[{tier}]", f"[EU {tier}]", f"[{tier} — EU SKIPPED]"}
    if tag not in tags:
        return False
    if key == "manual":
        return file == part and rest == MANUAL_TITLES.get(part)
    if file == FAST:
        if full:
            return False
        rest, n = re.subn(r"^Fast-lane deploy ", "Deploy ", rest)
        if not n:
            return False
        file = deploy.WORKFLOWS["function"]["file"]
        if key == "admin":
            return any(title_covers(f"function:{name}", env, file, f"{tag} {rest}", full=False)
                       for name in deploy.ADMIN_FUNCTIONS)
    if file == NOTICE:
        return not full and rest.startswith("Deploy Hosting with Notice")
    if file == RAPID and key == "hosting":
        return not full and rest.startswith("Deploy Rapid") and rest.endswith(" + core app hosting")
    if key == "function" and ("=" in rest or not rest.startswith("Deploy ")):
        if full:
            return False
    return deploy.run_covers(key, part or None, f"{deploy.WORKFLOWS[key]['tag']} {rest}", file)


def verify_run(g, target, env, run):
    """Exact code evidence from the successful publishing legs, including build/fast-lane jobs."""
    key, _, part = target.partition(":")
    family = part if key == "manual" else key
    sha = run.get("head_sha")
    try:
        jobs = g.jobs(run["id"], run.get("attempt") or 1)
        names = {job["name"]: job for job in jobs}
        if family == "universe":
            expected = ["Build and deploy Universe"]
        else:
            expected = [f"{JOB_NAMES[family]} ({region})" for region in regions(key, env)]
        publish_jobs = set(expected)
        if family == "hosting":
            expected += ["Prepare hosting bundle"]
        fast = [name for name in names if " / " in name and name.startswith("Fast lane (")]
        if family in {"function", "admin"} and fast:
            expected += [name for name in fast if name.endswith(" / Build once")]
            for region in regions(key, env):
                expected += [name for name in fast if name.endswith(f" / Deploy ({region})")]
            has_fast = any(name.endswith(" / Build once") for name in expected)
            if has_fast and not all(any(name.endswith(f" / Deploy ({region})") for name in expected)
                                    for region in regions(key, env)):
                return [], "The fast-lane region jobs could not be verified."
            if family == "function" and has_fast and any(
                    deploy.fn_key(part) in {deploy.fn_key(fn.removeprefix("functions:")) for fn in match.split(",")}
                    for match in (name.split(" / ", 1)[0].removeprefix("Fast lane (").removesuffix(")") for name in fast)):
                expected = [name for name in expected if name not in publish_jobs]
        publish_steps = PUBLISH_STEPS[family]
        if family == "rapid":
            publish_steps = (publish_steps[0 if part == "hosting" else 1],)
        evidence = []
        for name in expected:
            job = names.get(name)
            if not job or job["conclusion"] != "success":
                return evidence, f"Missing successful job: {name}."
            if name in publish_jobs and not set(publish_steps) <= {
                    step["name"] for step in job["steps"] if step["conclusion"] == "success"}:
                return evidence, f"The publishing steps in {name} could not be verified."
            checkout = g.checkout_sha(job["id"])
            evidence.append({"job": name, "id": job["id"], "checkout_sha": checkout})
        if not evidence or any(item["checkout_sha"] != sha for item in evidence):
            return evidence, "Deployment checkout SHA is missing or differs from the event SHA."
        return evidence, None
    except (deploy.GitHubError, KeyError):
        return [], "Deployment job evidence is unavailable."


def run_view(run, evidence=None):
    return {"id": run["id"], "url": run_link(run["id"]), "sha": run.get("head_sha"),
            "ref": run.get("head_branch"), "title": run.get("title"), "conclusion": run.get("conclusion"),
            "github_status": run.get("status"), "created": run.get("created"), "updated": run.get("updated"),
            "jobs": evidence or []}


def pr_run_may_overlap(g, run, target):
    if (run.get("title") or "").endswith(" (dry run)"):
        return False
    if run["status"] != "completed":
        return True
    key = target.split(":")[0]
    label = {"function": "Functions", "api": "API"}.get(key, key.title())
    jobs = g.jobs(run["id"], run.get("attempt") or 1)
    return any(job["name"].startswith(label + " /") and job["conclusion"] != "skipped" for job in jobs)


def coverage(g, target, env, commits):
    key = target.split(":")[0]
    if key == "manual" and target.split(":", 1)[1] not in MANUAL_TITLES:
        return None, None, "This manual workflow's exact inputs and coverage require operator review."
    found = []
    files = related_files(target)
    for file in files:
        if file == DEPLOY_PR:
            continue
        data = g.workflow_runs(file)
        for run in data["runs"]:
            if not title_covers(target, env, file, run.get("title"), full=False):
                continue
            found.append((run, file))
    if env == "staging" and DEPLOY_PR in files:
        newest = max((deploy.ms(run.get("created")) for run, _ in found), default=0)
        for run in g.workflow_runs(DEPLOY_PR)["runs"]:
            if (run["status"] != "completed" or deploy.ms(run.get("updated")) >= newest) and pr_run_may_overlap(g, run, target):
                found.append((run, DEPLOY_PR))
    found.sort(key=lambda item: (deploy.ms(item[0].get("created")), item[0]["id"]), reverse=True)
    for run, _ in found:
        if run["status"] != "completed":
            return None, run_view(run), "An overlapping workflow is still active."
    if not found:
        return None, None, "No overlapping deployment was found in the available workflow history."
    run, file = found[0]
    observed = run_view(run)
    if any(deploy.ms(other.get("updated")) > deploy.ms(run.get("created")) for other, _ in found[1:]):
        return None, observed, "Overlapping workflow completion order could not be verified."
    if run["conclusion"] != "success":
        return None, observed, "The latest overlapping workflow did not pass."
    if (run["head_branch"] != REF or run.get("event") != "workflow_dispatch" or run["head_sha"] not in commits
            or not title_covers(target, env, file, run["title"])):
        return None, observed, "The latest overlapping run cannot establish full coverage on master in this scan."
    if run.get("attempt", 1) != 1:
        return None, observed, "A rerun does not establish the order of its past deployments."
    evidence, problem = verify_run(g, target, env, run)
    observed["jobs"] = evidence
    return (observed, observed, None) if not problem else (None, observed, problem)


def read_workflow(g, file, head):
    try:
        return g.file(f".github/workflows/{file}", head)
    except deploy.GitHubError:
        return ""


def release_plan(g, selection, *, limit=SCAN_LIMIT):
    head = g.head(REF)
    floor, commits = g.history(head, limit)
    if not commits or commits[-1] != head:
        raise deploy.GitHubError("Master history is incomplete")
    index = g.file(deploy.ROBOFLOW["index"], head)
    functions = deploy.functions_at(index)
    entries = registrations(index)
    template = deploy.template_lines(g.file(deploy.ROBOFLOW["template"], head))
    sources = [parse_source(change, functions, entries, template) for change in parallel(g.change, commits)]
    targets = {target for source in sources for target in source["targets"]}
    edges = {tuple(edge) for source in sources for edge in source["manual_edges"]}
    # Preserve ordering internal to the 'both' deploy_target when sending parts separately.
    edges.update((f"{key}:functions", f"{key}:hosting") for key in ("api", "rapid"))
    # API functions and streaming preview workflows can deploy indexes as a side effect.
    edges.update((scope("manual", INDEXES), key) for key in targets
                 if key == "api:functions" or deploy.fn_key(key) == "function:light-v2-streamingpreview")
    if (scope("manual", TTL), "hosting") in edges and "hosting" in targets:
        targets.update({scope("manual", TTL), "function:light-v2-agent"})
    targets = sorted(targets)
    files = {target: related_files(target)[0] for target in targets}
    workflows = dict(parallel(lambda file: (file, read_workflow(g, file, head)), sorted(set(files.values()))))
    order = {sha: i for i, sha in enumerate([floor, *commits])}
    gaps = [source["commit"] for source in sources if source["issues"]]

    def one(pair):
        env, target = pair
        key = target.split(":")[0]
        unit, problem = unit_for(target, env, workflows[files[target]])
        try:
            baseline, observed, why = coverage(g, target, env, order)
        except deploy.GitHubError as error:
            baseline, observed, why = None, None, str(error)
        related = [source["commit"] for source in sources if target in source["targets"]]
        pending = [sha for sha in related if not baseline or order[sha] > order[baseline["sha"]]]
        if not pending and baseline:
            state, reason = "covered", None
        elif problem:
            state, reason = "unverified", problem
        elif key == "manual":
            state, reason = "manual", why
        elif not baseline:
            state, reason = ("running" if observed and observed["github_status"] != "completed" else "unverified"), why
        elif any(source["global_gap"] or (source["issues"] and target in source["targets"]) for source in sources):
            state, reason = "blocked", "Unresolved changes in the scan may affect this deployment."
        else:
            state, reason = "ready", None
        return {"target": target, "environment": env, "regions": target_regions(target, env),
                "ref": REF, "expected_head": head, "baseline": baseline, "observed_run": observed,
                "sources": related, "pending": pending, "state": state, "reason": reason,
                "depends_on": sorted(before for before, after in edges if after == target and before in targets),
                **unit}

    rows = parallel(one, [(env, target) for target in targets for env in ENVIRONMENTS[selection]])
    environments = {}
    for env in ENVIRONMENTS[selection]:
        by_target = {row["target"]: row for row in rows if row["environment"] == env}
        for row in by_target.values():
            if row["state"] != "covered":
                continue
            after = row["baseline"]
            for key in row["depends_on"]:
                before = by_target[key]["baseline"]
                if before and before["id"] == after["id"] and (key, row["target"]) in {
                        ("api:functions", "api:hosting"), ("rapid:functions", "rapid:hosting")}:
                    continue
                if not before or not 0 < deploy.ms(before["updated"]) <= deploy.ms(after["created"]):
                    row.update(state="unverified", reason=f"Prior deployment order with {key} could not be verified.")
                    break
        # Propagate ordering uncertainty through all prerequisite edges, regardless of row order.
        while any(row["state"] == "covered" and any(by_target[key]["state"] != "covered" for key in row["depends_on"])
                  for row in by_target.values()):
            for row in by_target.values():
                if row["state"] == "covered" and any(by_target[key]["state"] != "covered" for key in row["depends_on"]):
                    row.update(state="unverified", reason="A prerequisite's deployment coverage could not be verified.")
        for row in by_target.values():
            if row["state"] == "ready" and any(by_target[key]["state"] != "covered" for key in row["depends_on"]):
                row.update(state="waiting", reason="Waiting for verified coverage of prerequisite targets.")
        baselines = [row["baseline"]["sha"] for row in by_target.values() if row["baseline"]]
        state = "ready" if any(row["state"] == "ready" for row in by_target.values()) else "blocked"
        if not gaps and all(row["state"] == "covered" for row in by_target.values()):
            state = "no_deploy_needed"
        environments[env] = {"state": state, "baseline": min(baselines, key=order.get) if baselines else None,
                             "unresolved_changes": gaps, "targets": list(by_target.values())}
    if g.head(REF) != head:
        raise deploy.GitHubError("Master moved while planning. Request a new plan.")
    plan = {"schema": 1, "repository": REPO, "ref": REF, "head": head, "selection": selection,
            "scan": {"after": floor, "through": head, "limit": limit, "older_history_unverified": floor is not None},
            "sources": sources, "environments": environments}
    plan["plan_id"] = digest(plan)
    return plan

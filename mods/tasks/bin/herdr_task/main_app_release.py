"""Read-only release analysis for the Comando main app action."""

import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import tempfile
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from threading import BoundedSemaphore, Lock
from time import monotonic

from . import deploy
from .base import GH
from .github import gh
from .main_app_evidence import (
    JOB_LOG_BYTES, LOG_BYTES, VerifiedCheckouts, archive_checkouts, bounded_output, checkout_sha,
)

REPO = deploy.ROBOFLOW["repo"]
REF = deploy.ROBOFLOW["branch"]
ENVIRONMENTS = {"staging": ("staging",), "prod": ("prod",), "both": ("staging", "prod")}
SCAN_LIMIT = 32
PLAN_SECONDS = 20
READ_SECONDS = 6
READ_LIMIT = 120
READ_WORKERS = 12
FETCH_SECONDS = 3
LOG_SECONDS = 3.5
ARCHIVE_SECONDS = 2
SHA = re.compile(r"[0-9a-f]{40}")
ORIGIN = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)roboflow/roboflow(?:\.git)?",
                    re.I)
ROLLOUT = re.compile(r"^[+-].*(loginlessExampleDrafts|/query/solutions/loginless-example/|/agent-examples)", re.M)
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


def parallel(fn, seq, *, workers=READ_WORKERS):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, seq))


class PlanningError(deploy.GitHubError):
    """A transport or completeness failure that invalidates the entire plan."""


class ApprovedHeadMoved(deploy.GitHubError):
    """Master already differed from the approved head when revalidation began."""


class PlanReads:
    def __init__(self):
        self.start = monotonic()
        self.label = "master head"
        self.phases = {}
        self.calls = 0
        self.failure = None
        self.lock = Lock()
        self.slot = BoundedSemaphore(READ_WORKERS)

    @contextmanager
    def phase(self, label):
        start = monotonic()
        counts = defaultdict(lambda: {"calls": 0, "errors": 0, "elapsed_ms": 0})
        with self.lock:
            self.phases[label] = {"reads": counts}
            self.label = label
        try:
            yield
            self.remaining()
        finally:
            self.phases[label]["elapsed_ms"] = round(1000 * (monotonic() - start))

    def remaining(self):
        if self.failure:
            raise PlanningError(self.failure)
        left = PLAN_SECONDS - (monotonic() - self.start)
        if left <= 0:
            self.fail("Time limit reached. Check GitHub connectivity and try again.")
        return left

    def fail(self, reason):
        with self.lock:
            if not self.failure:
                self.failure = f"Deploy planning stopped during {self.label}. {reason}"
        raise PlanningError(self.failure)

    @contextmanager
    def read(self, kind, category):
        timeout = min(READ_SECONDS, self.remaining())
        start = monotonic()
        with self.lock:
            self.calls += kind == "github"
            over = self.calls > READ_LIMIT
            stats = self.phases[self.label]["reads"][f"{kind}: {category}"]
            if not over:
                stats["calls"] += 1
        if over:
            self.fail("GitHub read limit reached. Inspect recent Actions runs and retry once overlap has settled.")
        try:
            yield timeout
        except Exception:
            with self.lock:
                stats["errors"] += 1
            raise
        finally:
            with self.lock:
                stats["elapsed_ms"] += round(1000 * (monotonic() - start))

    def result(self):
        return {"elapsed_ms": round(1000 * (monotonic() - self.start)), "phases": self.phases}


def read_category(args):
    path = args[1].split("?", 1)[0]
    if path == "graphql":
        return "associated PR batches"
    if "/contents/" in path:
        return "files at master"
    if path.endswith("/jobs"):
        return "run jobs"
    if path.endswith("/logs"):
        return "checkout logs"
    if "/actions/workflows/" in path:
        return "workflow runs"
    return "commit metadata"


def hunks(patch):
    return [diff[diff.index("\n@@ "):] for diff in re.split(r"^diff --git ", patch, flags=re.M) if "\n@@ " in diff]


class LocalReleaseGit:
    """Use only immutable objects from the same primary checkout as Comando."""

    def __init__(self, reads):
        self.root = Path.home() / REPO
        self.cache = None
        self.reads = reads
        # Ignore ambient repository overrides, replacement refs, grafts and promisor fetches.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_GRAFT_FILE=os.devnull,
                        GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0")

    def git(self, category, *args):
        try:
            with self.reads.read("git", category) as timeout:
                return subprocess.check_output(["git", "-C", self.cache or self.root, *args], env=self.env, timeout=timeout,
                                               stderr=subprocess.DEVNULL).decode("utf-8")
        except (subprocess.SubprocessError, OSError, UnicodeError):
            return None

    def valid_checkout(self):
        if not self.root.is_dir():
            return False
        config = self.git("checkout identity", "config", "--local", "--get-regexp",
                          r"^(remote\.origin\.url|extensions\.partialclone|remote\..*\.promisor)$")
        root = self.git("checkout identity", "rev-parse", "--path-format=absolute", "--show-toplevel",
                        "--absolute-git-dir", "--git-common-dir", "--is-shallow-repository")
        origin = (config or "").strip().removeprefix("remote.origin.url ")
        return bool(ORIGIN.fullmatch(origin) and root == (
            f"{self.root}\n{self.root / '.git'}\n{self.root / '.git'}\nfalse\n"))

    def history(self, head, limit, *, scratch=None):
        if not self.valid_checkout():
            return None
        chain = self.first_parents(head, limit)
        if chain or scratch is None or not self.fetch(head, scratch):
            return chain
        return self.first_parents(head, limit)

    def first_parents(self, head, limit):
        chain = self.git("first parents", "rev-list", "--first-parent", f"--max-count={limit + 1}", head)
        shas = chain.splitlines() if chain else []
        if not shas or shas[0] != head or any(not SHA.fullmatch(sha) for sha in shas):
            return None
        return (shas[-1], list(reversed(shas[:-1]))) if len(shas) > limit else (None, list(reversed(shas)))

    def fetch(self, head, scratch):
        ref = f"refs/heads/{REF}"
        seed = self.git("checkout seed", "rev-parse", "--verify", f"refs/remotes/origin/{REF}^{{commit}}")
        if not SHA.fullmatch((seed or "").strip()):
            return False
        # Borrow read-only objects for negotiation. Only a recent delta for the canonical
        # master ref goes into this private, disposable bare repository.
        self.cache = Path(scratch.enter_context(tempfile.TemporaryDirectory(prefix="herdr-release-")))
        try:
            if self.git("history cache", "init", "--bare", "--template=") is None:
                return False
            (self.cache / "objects/info/alternates").write_text(str(self.root / ".git/objects") + "\n")
            if self.git("history cache", "update-ref", ref, seed.strip()) is None:
                return False
            with self.reads.read("git", "fetch master delta") as timeout:
                command = ["git", "-c", "credential.helper=!" + shlex.quote(GH) + " auth git-credential",
                           "-c", "http.followRedirects=false", "-c", "protocol.allow=never",
                           "-c", "protocol.https.allow=always", "fetch", "--no-auto-maintenance", "--no-tags",
                           "--no-write-fetch-head", "--recurse-submodules=no", f"--negotiation-tip={ref}",
                           f"https://github.com/{REPO}.git", f"+{ref}:{ref}"]
                code, _, _ = bounded_output(command, cwd=self.cache, env=self.env,
                                            timeout=min(timeout, FETCH_SECONDS), cap=64 * 1024)
                if code:
                    raise deploy.GitHubError("Git fetch failed")
            return self.git("fetched master identity", "rev-parse", "--verify", ref) == head + "\n"
        except PlanningError:
            raise
        except (OSError, subprocess.SubprocessError, deploy.GitHubError):
            return False

    def file(self, path, sha):
        result = self.git("files at master", "show", f"{sha}:{path}")
        return result.strip() if result is not None else None

    def change(self, sha, parent):
        # Use GitHub for the root diff or a missing tree.
        if not parent:
            return None
        args = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-relative", "--ignore-submodules=none",
                "--submodule=short", "--find-renames", "-l0", parent, sha, "--")
        names = self.git("first-parent paths", "diff", "--name-status", "-z", *args)
        if names is None:
            return None
        names = iter(names.removesuffix("\0").split("\0")) if names else iter(())
        files = []
        for status in names:
            source = next(names, None)
            if not source:
                return None
            if status.startswith(("R", "C")):
                dest = next(names, None)
                if not dest:
                    return None
                files.append({"filename": dest, "previous_filename": source})
            else:
                files.append({"filename": source})
        patch = self.git("first-parent rollout diff", "diff", "--patch", "--unified=0", *args) if files else ""
        if patch is None:
            return None
        rollout = any(ROLLOUT.search(diff) for diff in hunks(patch))
        for row in files:
            if "app/firebase.json" in row.values():
                firebase = self.git("firebase rollout diff", "diff", "--patch", "--unified=0", *args, "app/firebase.json")
                if firebase is None:
                    return None
                row["patch"] = "\n".join(hunks(firebase))
        return {"sha": sha, "files": files, "complete": True, "rollout": rollout}


class ReleaseGitHub(deploy.GitHub):
    def __init__(self, evidence_path=None):
        super().__init__(REPO)
        self.reads = None
        self.diagnostics = None
        self.local = None
        self.scratch = None
        self.evidence_pool = None
        self.verified = VerifiedCheckouts(evidence_path)
        self.pending = {}
        self.lock = Lock()

    @contextmanager
    def planning(self):
        self.reads = PlanReads()
        self.local = None
        self.pending.clear()
        try:
            with ExitStack() as self.scratch, ThreadPoolExecutor(max_workers=READ_WORKERS) as self.evidence_pool:
                yield self.reads.phase
        finally:
            self.diagnostics = self.reads.result()
            self.reads = None
            self.local = None
            self.scratch = None
            self.evidence_pool = None
            # Run listings, attempts and results are refreshed in each plan and subsequent status.
            self.pending.clear()

    def once(self, key, read):
        # lru_cache lets concurrent misses through. One caller owns each read for this pass.
        with self.lock:
            future = self.pending.get(key)
            owner = future is None
            if owner:
                future = self.pending[key] = Future()
        if owner:
            try:
                future.set_result(read())
            except BaseException as error:
                future.set_exception(error)
        return future.result()

    def call(self, *args):
        reads = self.reads
        category = read_category(args) if reads else None
        request = reads.read("github", category) if reads else nullcontext(30)
        optional = category in {"workflow runs", "run jobs", "checkout logs"} or (
            category == "files at master" and reads and reads.label == "workflow definitions")
        with (reads.slot if reads else nullcontext()), request as timeout:
            try:
                ok, out, error = gh(*args, "--hostname", "github.com", cwd="/", timeout=timeout)
            except subprocess.TimeoutExpired:
                if reads and not optional:
                    reads.fail("GitHub did not respond in time. Check connectivity and try again.")
                raise deploy.GitHubError("GitHub request timed out") from None
            if not ok:
                # Report safe diagnostics, without returning response bodies, logs or credentials.
                status = re.search(r"\bHTTP [1-5][0-9]{2}\b", error)
                message = status.group() if status else "GitHub request failed"
                if reads and not optional:
                    if re.search(r"rate.limit", error, re.I) or message == "HTTP 429":
                        reads.fail("GitHub rate limited the reads. Retry after the limit resets.")
                    if message in {"HTTP 401", "HTTP 403", "HTTP 404"} or "gh auth login" in error or "FORBIDDEN" in error:
                        reads.fail(f"{message}. Check gh sign-in and repository, pull request and Actions read access.")
                    reads.fail(f"{message}. Check gh connectivity and access, then try again.")
                raise deploy.GitHubError(message)
        return out

    def api(self, path, *args):
        return self.json("api", f"repos/{REPO}/{path}", *args)

    def head(self, branch):
        sha = self.call("api", f"repos/{REPO}/git/ref/heads/{branch}", "--jq", ".object.sha")
        if not SHA.fullmatch(sha):
            raise deploy.GitHubError(f"{branch}'s head unreadable")
        return sha

    def history(self, head, limit):
        if self.reads:
            local = LocalReleaseGit(self.reads)
            chain = local.history(head, limit, scratch=self.scratch)
            if chain:
                self.local = local
                return chain
        # REST lists the graph, so follow only explicit first-parent edges, fetching parents
        # omitted from the page (for example after a merge with a large side branch).
        page = self.api(f"commits?sha={head}&per_page=100", "--jq", "[.[] | {sha, parents: [.parents[] | {sha}]}]")
        commits = {row["sha"]: row for row in page}
        landed = []
        while head and len(landed) < limit:
            row = commits.get(head) or self.api(f"git/commits/{head}")
            parents = row["parents"]
            parent = parents[0]["sha"] if parents else None
            landed.append(head)
            head = parent
        return head, list(reversed(landed))

    def changed_files(self, sha, parent):
        local = self.local.change(sha, parent) if self.local else None
        if local is not None:
            return local
        # GET a merge commit's diff against its first parent. GitHub caps returned files at 3000.
        pages = self.api(f"commits/{sha}?per_page=100", "--paginate", "--slurp")
        files = [row for page in pages for row in page["files"]]
        identity = all(page.get("sha") == sha and (page["parents"][0]["sha"] if page["parents"] else None) == parent
                       for page in pages)
        # A truncated/missing patch cannot rule out a required rollout. Check the complete
        # first-parent delta instead of treating an omitted patch as an empty patch.
        patches = all(sum(line.startswith(("+", "-")) for line in (row.get("patch") or "").split("\n"))
                      == row.get("changes") for row in files)
        return {"sha": sha, "files": files if identity else [],
                "complete": bool(pages) and identity and patches and len(files) < 3000}

    def associated_prs(self, shas):
        fields = ("oid associatedPullRequests(first: 20) { pageInfo { hasNextPage } "
                  "nodes { number body url mergedAt mergeCommit { oid } baseRefName baseRepository { nameWithOwner } } }")
        aliases = [f'c{i}: object(oid: "{sha}") {{ ... on Commit {{ {fields} }} }}' for i, sha in enumerate(shas)]
        query = 'query { repository(owner: "roboflow", name: "roboflow") { ' + " ".join(aliases) + " } }"
        response = self.json("api", "graphql", "-f", "query=" + query)
        if not isinstance(response, dict) or response.get("errors") or not (response.get("data") or {}).get("repository"):
            self.reads.fail("GitHub did not return PR associations. Check repository and pull request read access.")
        repo = response["data"]["repository"]
        result = {}
        for i, sha in enumerate(shas):
            commit = repo[f"c{i}"]
            if not commit or commit["oid"] != sha:
                self.reads.fail("GitHub did not return the requested master commits. Try again.")
            connection = commit["associatedPullRequests"]
            if connection["pageInfo"]["hasNextPage"]:
                self.reads.fail("A master commit has too many associated PRs to verify. Inspect the commit in GitHub.")
            exact = [pr for pr in connection["nodes"] if (pr.get("mergeCommit") or {}).get("oid") == sha
                     and pr.get("mergedAt") and pr["baseRefName"] == REF
                     and (pr.get("baseRepository") or {}).get("nameWithOwner", "").lower() == REPO]
            result[sha] = {"number": exact[0]["number"], "body": exact[0]["body"],
                           "html_url": exact[0]["url"]} if len(exact) == 1 else None
        return result

    def landed_changes(self, floor, commits, on_paths):
        batches = [commits[i:i + 8] for i in range(0, len(commits), 8)]
        parents = dict(zip(commits, [floor, *commits]))
        # Share a bounded pool between the PR batches and commit diffs.
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            prs = [pool.submit(self.associated_prs, batch) for batch in batches]
            files = [pool.submit(self.changed_files, sha, parents[sha]) for sha in commits]
            for job in as_completed(files):
                on_paths(job.result()["files"])
            joined = {sha: pr for batch in prs for sha, pr in batch.result().items()}
            return [{**item, "pr": joined[item["sha"]]} for item in (job.result() for job in files)]

    def file(self, path, ref):
        local = self.local.file(path, ref) if self.local else None
        return local if local is not None else super().file(path, ref)

    def workflow_runs(self, file):
        # Across branches and events: staging PR deployments and side workflows can overlap.
        return self.once(("runs", file), lambda: self.api(f"actions/workflows/{file}/runs?per_page=100", "--jq",
                                                         f'{{total: .total_count, runs: [.workflow_runs[] | {RUN_FIELDS}]}}'))

    def exact_run(self, run_id):
        return self.api(f"actions/runs/{int(run_id)}", "--jq", RUN_FIELDS)

    def jobs(self, run_id, attempt):
        def read():
            pages = self.api(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", "--paginate", "--slurp")
            return [job for page in pages for job in page["jobs"]]

        return self.once(("jobs", run_id, attempt), read)

    def checkout_sha(self, job_id):
        return self.once(("checkout", job_id), lambda: self.read_checkout_sha(job_id))

    def log(self, path, cap, category, *, seconds=None):
        reads = self.reads
        request = reads.read("github", category) if reads else nullcontext(LOG_SECONDS)
        with (reads.slot if reads else nullcontext()), request as timeout:
            try:
                code, data, error = bounded_output(
                    [GH, "api", f"repos/{REPO}/{path}", "--hostname", "github.com"],
                    cwd="/", timeout=min(timeout, seconds or LOG_SECONDS), cap=cap)
            except (OSError, subprocess.TimeoutExpired):
                raise deploy.GitHubError("GitHub logs did not respond in time.") from None
            if code:
                status = re.search(rb"\bHTTP [1-5][0-9]{2}\b", error)
                raise deploy.GitHubError(status[0].decode() if status else "GitHub logs are unavailable.")
        return data

    def checkouts(self, run, jobs, required):
        run_id, attempt = int(run["id"]), int(run.get("attempt") or 1)
        archived = {}
        if len(required) > 1:
            try:
                archive = self.once(("archive", run_id, attempt), lambda: self.log(
                    f"actions/runs/{run_id}/attempts/{attempt}/logs", LOG_BYTES, "attempt log archives",
                    seconds=ARCHIVE_SECONDS))
                archived = archive_checkouts(archive, jobs, required)
            except PlanningError:
                raise
            except deploy.GitHubError:
                # The precise job ID endpoint also covers archives with ambiguous name mappings.
                pass

        def direct(job_id):
            try:
                return self.checkout_sha(job_id)
            except PlanningError:
                raise
            except deploy.GitHubError:
                return None

        missing = [job["id"] for job in required if job["id"] not in archived]
        archived.update(zip(missing, self.evidence(direct, missing)))
        return [archived[job["id"]] for job in required]

    def evidence(self, read, items):
        if not self.evidence_pool:
            return [read(item) for item in items]
        jobs = [self.evidence_pool.submit(read, item) for item in items]
        wait(jobs)
        return [job.result() for job in jobs]

    def read_checkout_sha(self, job_id):
        return checkout_sha(self.log(f"actions/jobs/{int(job_id)}/logs", JOB_LOG_BYTES, "checkout logs"))


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
        errors.append("The first-parent changed files or patches are incomplete.")
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
    rollout = change.get("rollout") or any(ROLLOUT.search(patch) for patch in patches)
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
    sha = run.get("head_sha")
    if (run.get("status") != "completed" or run.get("conclusion") != "success" or
            not SHA.fullmatch(sha or "") or not run.get("attempt")):
        return [], "The exact successful deployment attempt could not be verified."
    # This version identifies the job/step requirements and checkout parsing policy.
    # Revalidation still applies the latest workflow listing, overlap and sequencing rules.
    key = digest([1, REPO, target, env, run["id"], run["attempt"], sha, run.get("path"), run.get("title")])
    cached = g.verified.get(key, sha)
    if cached:
        return cached, None
    evidence, problem = verify_jobs(g, target, env, run)
    if not problem:
        g.verified.put(key, evidence)
    return evidence, problem


def verify_jobs(g, target, env, run):
    key, _, part = target.partition(":")
    family = part if key == "manual" else key
    sha = run.get("head_sha")
    try:
        jobs = g.jobs(run["id"], run.get("attempt") or 1)
        names = defaultdict(list)
        for job in jobs:
            names[job["name"]].append(job)
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
        required = []
        for name in expected:
            matches = names.get(name, [])
            if len(matches) != 1 or matches[0]["conclusion"] != "success":
                return [], f"Missing successful job: {name}."
            job = matches[0]
            if name in publish_jobs and not set(publish_steps) <= {
                    step["name"] for step in job["steps"] if step["conclusion"] == "success"}:
                return [], f"The publishing steps in {name} could not be verified."
            required.append(job)
        if any(type(job["id"]) is not int or job["id"] <= 0 for job in required) or (
                len({job["id"] for job in required}) != len(required)):
            return [], "The publishing job identities could not be verified."
        checkouts = g.checkouts(run, jobs, required)
        evidence = [{"job": job["name"], "id": job["id"], "checkout_sha": checkout}
                    for job, checkout in zip(required, checkouts)]
        if not evidence or any(item["checkout_sha"] != sha for item in evidence):
            return evidence, "Deployment checkout SHA is missing or differs from the event SHA."
        return evidence, None
    except PlanningError:
        raise
    except deploy.GitHubError as error:
        return [], f"Deployment job evidence is unavailable. {error}"
    except (KeyError, TypeError):
        return [], "Deployment job evidence is unreadable."


def run_view(run, evidence=None):
    return {"id": run["id"], "url": run_link(run["id"]), "sha": run.get("head_sha"),
            "ref": run.get("head_branch"), "title": run.get("title"), "conclusion": run.get("conclusion"),
            "github_status": run.get("status"), "created": run.get("created"), "updated": run.get("updated"),
            "jobs": evidence or []}


def pr_run_may_overlap(g, run, target):
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
    def order(run):
        return deploy.ms(run.get("created")), run["id"]

    pr_runs = []
    if env == "staging" and DEPLOY_PR in files:
        pr_runs = [run for run in g.workflow_runs(DEPLOY_PR)["runs"]
                   if not (run.get("title") or "").endswith(" (dry run)")]
    active = [run for run, _ in found if run["status"] != "completed"]
    active.extend(run for run in pr_runs if run["status"] != "completed")
    if active:
        return None, run_view(max(active, key=order)), "An overlapping workflow is still active."
    if not found:
        return None, None, "No full-tier master baseline was found in the available workflow history."
    found.sort(key=lambda item: order(item[0]), reverse=True)
    run, file = found[0]
    observed = run_view(run)
    created = order(run)[0]
    if any(deploy.ms(other.get("updated")) > created for other, _ in found[1:]):
        return None, observed, "Overlapping workflow completion order could not be verified."
    if run["conclusion"] != "success":
        return None, observed, "The latest overlapping workflow did not pass."
    if (run["head_branch"] != REF or run.get("event") != "workflow_dispatch" or run["head_sha"] not in commits
            or run.get("path", "").split("@")[0] != f".github/workflows/{file}"
            or not title_covers(target, env, file, run["title"])):
        return None, observed, "The latest overlapping run cannot establish full coverage on master in this scan."
    if run.get("attempt", 1) != 1:
        return None, observed, "A rerun does not establish the order of its past deployments."
    # Completed PR deploys can disqualify a possible baseline, never establish one. Once a
    # conflicting run is proven, older PR jobs cannot restore coverage.
    candidates = [pr for pr in sorted(pr_runs, key=order, reverse=True)
                  if deploy.ms(pr.get("updated")) > created
                  or (deploy.ms(pr.get("updated")) == created and order(pr) > order(run))]
    for start in range(0, len(candidates), 4):
        batch = candidates[start:start + 4]
        overlaps = g.evidence(lambda pr: pr_run_may_overlap(g, pr, target), batch)
        for pr, overlap in zip(batch, overlaps):
            if not overlap:
                continue
            if order(pr) > order(run):
                return None, run_view(pr), "The latest overlapping run cannot establish full coverage on master in this scan."
            return None, observed, "Overlapping workflow completion order could not be verified."
    evidence, problem = verify_run(g, target, env, run)
    observed["jobs"] = evidence
    return (observed, observed, None) if not problem else (None, observed, problem)


def read_workflow(g, file, head):
    try:
        return g.file(f".github/workflows/{file}", head)
    except PlanningError:
        raise
    except deploy.GitHubError:
        return ""


def release_plan(g, selection, *, limit=SCAN_LIMIT, expected_head=None):
    progress = {}
    try:
        with g.planning() as phase, ThreadPoolExecutor(max_workers=READ_WORKERS) as run_pool:
            return build_plan(g, selection, limit, expected_head, phase, progress, run_pool)
    except deploy.GitHubError as error:
        if "scan" in progress:
            results = progress.pop("results", {})
            targets = progress.pop("targets", [])
            progress["environments"] = {}
            for env in ENVIRONMENTS[selection]:
                rows = []
                for target in targets:
                    row = results.get((env, target))
                    if row is None:
                        row = {"environment": env, "target": target, "baseline": None, "state": "unverified",
                               "reason": "Deployment coverage was not checked."}
                    elif row["state"] in {"ready", "waiting", "covered"}:
                        # Local observations may be intact but final head and ordering checks did not finish.
                        row = {**row, "state": "blocked", "reason": "The full plan could not be verified."}
                    rows.append(row)
                progress["environments"][env] = {"state": "blocked", "targets": rows}
            error.partial = progress
        raise


def build_plan(g, selection, limit, expected_head, phase, progress, run_pool):
    with phase("master head"):
        head = g.head(REF)
        if expected_head is not None and head != expected_head:
            raise ApprovedHeadMoved("Master moved since the plan was approved.")
    with phase("first-parent history"):
        floor, commits = g.history(head, limit)
        chain = [*([floor] if floor else []), *commits]
        if (not commits or commits[-1] != head or len(chain) != len(set(chain))
                or any(not SHA.fullmatch(sha) for sha in chain)):
            raise deploy.GitHubError("Master history is incomplete. Check GitHub and try again.")
        scan = {"after": floor, "through": head, "limit": limit, "older_history_unverified": floor is not None}
        progress.update(repository=REPO, ref=REF, head=head, selection=selection, scan=scan)
    scheduled = set()

    def prefetch(targets):
        for target in targets:
            if target.startswith("manual:") and target.split(":", 1)[1] not in MANUAL_TITLES:
                continue
            for file in related_files(target):
                if file not in scheduled and (file != DEPLOY_PR or "staging" in ENVIRONMENTS[selection]):
                    scheduled.add(file)
                    run_pool.submit(g.workflow_runs, file)

    with phase("merged changes and deploy instructions"):
        index_path, template_path = (deploy.ROBOFLOW[name] for name in ("index", "template"))
        index, template_text = parallel(lambda path: g.file(path, head), (index_path, template_path))
        functions = deploy.functions_at(index)
        entries = registrations(index)
        template = deploy.template_lines(template_text)
        def prefetch_paths(files):
            # Direct ownership already establishes these targets while PR reads are in progress.
            for row in files:
                for path in (row["filename"], row.get("previous_filename")):
                    if path:
                        prefetch((path_scope(path, entries, row.get("patch") or "") or set()) - {"@backend"})

        changes = g.landed_changes(floor, commits, prefetch_paths)
        sources = [parse_source(change, functions, entries, template) for change in changes]
        progress["sources"] = sources
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
    progress["targets"] = targets
    files = {target: related_files(target)[0] for target in targets}
    prefetch(targets)
    with phase("workflow definitions"):
        workflows = dict(parallel(lambda file: (file, read_workflow(g, file, head)), sorted(set(files.values()))))
    order = {sha: i for i, sha in enumerate([floor, *commits])}
    gaps = [source["commit"] for source in sources if source["issues"]]

    results = progress["results"] = {}

    def one(pair):
        env, target = pair
        key = target.split(":")[0]
        unit, problem = unit_for(target, env, workflows[files[target]])
        try:
            baseline, observed, why = coverage(g, target, env, order)
        except PlanningError:
            raise
        except deploy.GitHubError as error:
            baseline, observed, why = None, None, str(error)
        except (KeyError, TypeError, ValueError):
            baseline, observed, why = None, None, "GitHub returned unreadable deployment coverage."
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
        row = {"target": target, "environment": env, "regions": target_regions(target, env),
               "ref": REF, "expected_head": head, "baseline": baseline, "observed_run": observed,
               "sources": related, "pending": pending, "state": state, "reason": reason,
               "depends_on": sorted(before for before, after in edges if after == target and before in targets),
               **unit}
        results[pair] = row
        return row

    with phase("deployment coverage"):
        # Each target consumes its own snapshots and needn't wait for the slowest list.
        # Several targets share one run or archive. Let other targets start while they
        # wait for that evidence, keeping network reads under the shared semaphore.
        rows = parallel(one, [(env, target) for target in targets for env in ENVIRONMENTS[selection]],
                        workers=3 * READ_WORKERS)
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
    with phase("master head recheck"):
        if g.head(REF) != head:
            raise deploy.GitHubError("Master moved while planning. Request a new plan.")
        plan = {"schema": 1, "repository": REPO, "ref": REF, "head": head, "selection": selection,
                "scan": scan, "sources": sources, "environments": environments}
        plan["plan_id"] = digest(plan)
        return plan

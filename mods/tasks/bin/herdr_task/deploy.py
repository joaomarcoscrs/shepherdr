"""SHEP-16: after the merge, Awaiting deploy (roboflow/roboflow Open PR hand-offs only).

An Open PR hand-off (SHEP-7) the person took waits in Awaiting review (review.py) until GitHub says its pull request
merged. For a repo in CONFIGS (only roboflow/roboflow) that merge no longer marks it Done: it moves to its own ongoing
state, "deploying" (Awaiting deploy), until the deploys its pull request lists are verified successful on GitHub, its
section says plainly that it needs none, or the person marks it Done (`herdr-task done`). Every other repo, and every
other delivery, finishes at the merge as before.

What it needs comes from the pull request's "Github Actions Required to Deploy" section, read by a strict parser
(plan()), never by a model and never run: each line is a `functions:` list (docs/pr-conventions.md's one
comma-separated deploy_only line), a workflow named in WORKFLOWS' words, a workflow Herdr never dispatches (MANUAL),
or an explicit "None" (NO_DEPLOY). Anything else, a missing or empty section, or a template left as it was, never
counts as "no deploy": the plan is unclear (Needs a manual step) or unavailable. Function names are checked against
app/functions/index.js at the commit that landed; light-v2-api and the universe/rapid servers deploy through their
own workflows (the conventions' mapping), so naming them as functions is unclear, not translated.

Deploy PR (deploy-pr.yaml) deploys an open PR's branch to staging; it is never used here. After the merge the deploys
are the ones a person dispatches on master to prod (US + EU): Deploy Function, Deploy API, Deploy Hosting, ... with
force and skip_eu sent as false, their dispatch inputs checked against the workflow's definition on master first.

Landing: a merge into master is the landing. A pull request stacked on another branch (ROBO-9 on
joao/evals/agent-experience-bundle) has not landed when it merges: it waits ("waiting") until master holds its merge
commit, or a pull request from its base branch whose head held that commit merged into master (that merge, squash or
not, is then the landing), up to STACK_DEPTH branches down. A deploy counts only from a run on master whose head holds
the landing commit, its title naming the target in prod (a newer run of another function never does).

Dispatch is the person's own act: `herdr-task deploy <id>` previews the exact workflow, ref, environment and inputs,
and `--yes` dispatches them, only when the plan is clear and verified, everything re-read from GitHub at that moment,
and no matching run is already running or submitted. Each dispatch is kept with its run (GitHub's return_run_details,
else the one run of that title by the same person just after it); submitted is never passed. When Herdr can't
dispatch safely, each target keeps a link to its workflow on GitHub to run by hand.

The record, kept on the task (the contract the CLI, the Tasks panel, the web app and a later cockpit read):

  t["state"]  "deploying"
  t["deploy"] = {
    state      waiting | unavailable | manual | ready | running | failed | success | none | unknown
    text       those in words: TEXT ("GitHub unavailable" while reads fail, was: the last state)
    repo, pr, url, base, merge_commit, merged_at, since
    landing    {state: waiting | landed, sha, at, via: {pr, url, branch, state} | None, why, branch}
    plan       {state: ready | manual | unavailable | none, why, section, unclear [lines], notes, hash, ref, read_at}
    targets    [{key, workflow, file, label, environment, ref, status, why, run, workflow_url, by}]
               status: waiting | ready | submitted | running | failed | success | manual | unverified
               run: {id, url, status, conclusion, head_sha, created, actor, title, by: "herdr" | "other"}
    dispatches [{at, workflow, file, ref, ref_sha, inputs, targets, run_id, url, ok, error, by}]
    checked, ok_at, fails, error, next, was
  }
"""

import hashlib
import json
import re
from datetime import datetime

from .base import now
from .github import gh

DEPLOYING = "deploying"

TEXT = {"waiting": "Waiting for master", "unavailable": "Deploy plan unavailable", "manual": "Needs a manual step",
        "ready": "Ready to deploy", "running": "Deploying", "failed": "Deploy failed", "success": "Deployed",
        "none": "No deploy needed", "unknown": "Checking GitHub", "github": "GitHub unavailable"}
TARGET_TEXT = {"waiting": "waits for master", "ready": "not run yet", "submitted": "submitted, no run yet",
               "running": "running", "failed": "failed", "success": "passed", "manual": "by hand",
               "unverified": "unverified"}

# ---- trusted configuration: the only repo, workflows and inputs Herdr reads or dispatches -------------------------

PROD = "[Prod + EU Prod]"
WORKFLOWS = {
    # key: the workflow file on master, its name, its run's title in prod, the inputs Herdr sends (fixed values, plus
    # deploy_only / deploy_target from the plan); every other boolean it defines stays at its default, checked false.
    "function": {"file": "deploy-function.yaml", "name": "Deploy Function", "tag": PROD, "label": "Deploy ${{ inputs.deploy_only }}",
                 "send": {"environment": "prod", "force": "false", "skip_eu": "false"}, "needs": ("deploy_only",)},
    "api": {"file": "deploy-api.yaml", "name": "Deploy API", "tag": PROD, "label": "Deploy API",
            "send": {"environment": "prod", "force": "false", "skip_eu": "false"}, "needs": ("deploy_target",), "parts": True},
    "hosting": {"file": "deploy-hosting.yaml", "name": "Deploy Hosting", "tag": PROD, "label": "Deploy Hosting",
                "send": {"environment": "prod", "skip_eu": "false"}},
    "universe": {"file": "deploy-universe.yaml", "name": "Deploy Universe", "tag": "[Prod]", "label": "Deploy Universe",
                 "send": {"environment": "prod", "force": "false"}},
    "admin": {"file": "deploy-admin.yaml", "name": "Deploy Admin", "tag": PROD, "label": "Deploy Admin",
              "send": {"environment": "prod", "force": "false", "skip_eu": "false"}},
    "public": {"file": "deploy-public.yaml", "name": "Deploy Public", "tag": PROD, "label": "Deploy Public",
               "send": {"environment": "prod", "skip_eu": "false"}},
    "rapid": {"file": "deploy-rapid.yaml", "name": "Deploy Rapid", "tag": PROD, "label": "Deploy Rapid",
              "send": {"environment": "prod", "force": "false", "skip_eu": "false", "deploy_core_app_hosting": "false"},
              "needs": ("deploy_target",), "parts": True},
}
# Other runs that deploy the same thing (read, never dispatched): Deploy Hosting with Notice deploys hosting too.
ALSO = {"hosting": [{"file": "frontend-refresh-notice.yaml", "prefix": PROD + " Deploy Hosting with Notice"}]}
PART_SUFFIX = {"both": "", "hosting": " (hosting only)", "functions": " (functions only)"}

# A line's words (lower case, "deploy " and " workflow" taken off) -> (workflow, parts).
WORDS = {"api": ("api", ("functions", "hosting")), "api (both)": ("api", ("functions", "hosting")),
         "api hosting": ("api", ("hosting",)), "api (hosting only)": ("api", ("hosting",)), "api hosting only": ("api", ("hosting",)),
         "api functions": ("api", ("functions",)), "api (functions only)": ("api", ("functions",)), "api functions only": ("api", ("functions",)),
         "hosting": ("hosting", None), "app hosting": ("hosting", None),
         "universe": ("universe", None), "admin": ("admin", None), "public": ("public", None),
         "rapid": ("rapid", ("functions", "hosting")), "rapid (both)": ("rapid", ("functions", "hosting")),
         "rapid hosting": ("rapid", ("hosting",)), "rapid (hosting only)": ("rapid", ("hosting",)),
         "rapid functions": ("rapid", ("functions",)), "rapid (functions only)": ("rapid", ("functions",))}
# Workflows a pull request may list that Herdr never dispatches (their own inputs and order are the person's call): a
# manual step, linked.
MANUAL = {"rules": "deploy-rules.yaml", "firestore rules": "deploy-rules.yaml", "storage rules": "deploy-rules.yaml",
          "database rules": "deploy-rules.yaml", "indexes": "deploy-indexes.yaml", "firestore indexes": "deploy-indexes.yaml",
          "ttl": "deploy-ttl.yaml", "embed": "deploy-embed.yaml", "demo": "deploy-demo.yaml", "referrals": "deploy-referrals.yaml",
          "agent redirects": "deploy-agent-redirects.yaml", "gsutil cors": "deploy-gsutil-cors.yaml", "cors": "deploy-gsutil-cors.yaml",
          "spanner migrate": "spanner-migrate.yaml", "spanner migration": "spanner-migrate.yaml",
          "hosting with notice": "frontend-refresh-notice.yaml", "function fast lane": "deploy-function-fast-lane.yaml",
          "adjust function traffic": "adjust-function-traffic.yaml", "run ops script": "run-ops-script.yaml"}
# An explicit "this needs no deploy", the whole section: nothing else counts as none.
NO_DEPLOY = {"none", "n/a", "na", "nothing", "nothing to deploy", "none needed", "none required", "not needed",
             "no deploy", "no deploys", "no deploy needed", "no deploys needed", "no deploy required", "no deploys required",
             "no deployment needed", "no deployment required", "no deployments needed", "no deployments required",
             "no github actions needed", "no github actions required", "no actions needed", "no actions required"}
# docs/pr-conventions.md: light-v2-api deploys through the API workflow, the universe and rapid servers through theirs
# (validate-function-names.sh refuses them); the API and Admin workflows deploy these functions themselves.
VIA_WORKFLOW = {"light-v2-api": "api", "universe-v2-server": "universe", "rapid-v2-server": "rapid"}
API_FUNCTIONS = ("light-v2-api", "light-v2-video", "light-v2-videojobreaper", "light-v2-device", "light-v2-deviceconfig")
ADMIN_FUNCTIONS = ("light-v2-admin", "light-v2-adminlong")
# The template's own words in the section (it asks for the answer in place of YOUR_ANSWER): never deploy text.
TEMPLATE_LINES = ("List exactly which deploys this change needs. The Deploy PR workflow reads this section as the",
                  "deploy list, so an example left here is deployed to staging.",
                  "Firebase functions go on one line, comma-separated, each name prefixed with `functions:` (the value",
                  "typed into the functions deploy action). Put every other workflow on its own line.")
PLACEHOLDER = "YOUR_ANSWER"

ROBOFLOW = {"repo": "roboflow/roboflow", "branch": "master", "environment": "prod", "index": "app/functions/index.js",
            "template": ".github/pull_request_template.md"}
CONFIGS = {"roboflow/roboflow": ROBOFLOW}

STACK_DEPTH = 3                 # branches between a stacked pull request and master, at most
WAIT_EVERY = 10 * 60000         # not on master yet: read again this often
READY_EVERY = 5 * 60000         # ready, failed: someone may run it by hand
MANUAL_EVERY = 15 * 60000       # unclear, unavailable
RUNNING_EVERY = 60000           # a run going on
PLAN_EVERY = 6 * 3600000        # the plan's workflows and functions checked again at least this often
RETRY_FIRST = 60000
RETRY_MAX = 30 * 60000
UNAVAILABLE_AFTER = 3
SUBMIT_WINDOW = 10 * 60000      # a dispatch with no run id: its run is the one that started this soon after, by us
RUNS_PAGE = 60


class GitHubError(Exception):
    pass


def config_for(repo):
    return CONFIGS.get(str(repo or "").lower())


def deploying(t):
    return t.get("state") == DEPLOYING


def applies(t):
    """An Open PR hand-off of a repo whose deploys Herdr follows (CONFIGS)."""
    h = t.get("handoff") or {}
    if not (h.get("pr") and config_for(h.get("repo"))):
        return False
    from .settings import open_pr
    return open_pr(t)


def workflow_url(repo, file):
    return f"https://github.com/{repo}/actions/workflows/{file}"


def ms(iso):
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def fn_key(name):
    """A function's name as firebase and the validator compare it: dots as hyphens, any case."""
    return str(name).replace(".", "-").lower()


# ---- GitHub: every read and the one write, through gh (the person's own sign-in) ------------------------------------

class GitHub:
    """One pass's reads of one repo, cached for the pass. Any failed read raises GitHubError."""

    def __init__(self, repo, seen=None):
        self.repo, self.cache, self.seen = repo, {}, seen if seen is not None else {}

    def call(self, *args):
        ok, out, err = gh(*args)
        if not ok:
            raise GitHubError(" ".join((err or "gh failed").split())[:200])
        return out

    def json(self, *args):
        out = self.call(*args)
        try:
            return json.loads(out) if out else None
        except ValueError:
            raise GitHubError(f"gh gave no JSON for {' '.join(args[:3])}")

    def pr(self, number):
        k = ("pr", int(number))
        if k not in self.cache:
            v = self.json("pr", "view", str(int(number)), "--repo", self.repo, "--json",
                          "number,state,baseRefName,headRefName,headRefOid,mergeCommit,mergedAt,body,url")
            if not isinstance(v, dict) or not v.get("state"):
                raise GitHubError(f"pull request #{number} unreadable")
            self.cache[k] = v
        return self.cache[k]

    def prs_from(self, branch):
        """The pull requests whose head is branch (any state, newest first)."""
        v = self.json("pr", "list", "--repo", self.repo, "--head", branch, "--state", "all", "--limit", "20", "--json",
                      "number,state,baseRefName,headRefOid,mergeCommit,mergedAt,url")
        return sorted([p for p in v or [] if isinstance(p, dict)], key=lambda p: -int(p.get("number") or 0))

    def contains(self, sha, ref):
        """ref (a branch or a commit) holds commit sha. A commit pair's answer never changes: kept in seen."""
        key = f"{sha}..{ref}"
        if re.fullmatch(r"[0-9a-f]{40}", str(ref)) and key in self.seen:
            return self.seen[key]
        st = self.call("api", f"repos/{self.repo}/compare/{sha}...{ref}", "--jq", ".status")
        yes = st.strip() in ("ahead", "identical")
        if re.fullmatch(r"[0-9a-f]{40}", str(ref)):
            self.seen[key] = yes
        return yes

    def head(self, branch):
        sha = self.call("api", f"repos/{self.repo}/commits/{branch}", "--jq", ".sha").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubError(f"{branch}'s head unreadable")
        return sha

    def file(self, path, ref):
        k = ("file", path, ref)
        if k not in self.cache:
            self.cache[k] = self.call("api", f"repos/{self.repo}/contents/{path}?ref={ref}", "-H", "Accept: application/vnd.github.raw")
        return self.cache[k]

    def runs(self, file, branch):
        k = ("runs", file, branch)
        if k not in self.cache:
            out = self.call("api", f"repos/{self.repo}/actions/workflows/{file}/runs?branch={branch}&event=workflow_dispatch&per_page={RUNS_PAGE}",
                            "--jq", ".workflow_runs[] | {id, title: .display_title, head_sha, head_branch, status, conclusion, "
                                    "created: .created_at, url: .html_url, actor: .actor.login, event}")
            rows = []
            for ln in out.splitlines():
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("head_branch") == branch and r.get("event") == "workflow_dispatch":
                    rows.append(r)
            self.cache[k] = rows
        return self.cache[k]

    def login(self):
        if "login" not in self.cache:
            self.cache["login"] = self.call("api", "user", "--jq", ".login").strip()
        return self.cache["login"]

    def dispatch(self, file, ref, inputs, *, api_version=None):
        """POST the workflow_dispatch: (run id or None, run url or None)."""
        args = ["api", "-X", "POST", f"repos/{self.repo}/actions/workflows/{file}/dispatches", "-f", f"ref={ref}"]
        args += ["-H", f"X-GitHub-Api-Version: {api_version}"] if api_version else ["-F", "return_run_details=true"]
        for k, v in inputs.items():
            args += ["-f", f"inputs[{k}]={v}"]
        out = self.call(*args)
        try:
            v = json.loads(out) if out.strip() else {}
        except ValueError:
            v = {}
        rid = v.get("workflow_run_id") if isinstance(v, dict) else None
        return (int(rid) if str(rid or "").isdigit() else None), (v.get("html_url") if isinstance(v, dict) else None)


# ---- the plan: the pull request's section, strictly ----------------------------------------------------------------

def deploy_section(body):
    """Its "Github Actions Required to Deploy" section, comments out (as Deploy PR's extractDeploySection), or None
    when there is no such heading."""
    lines = (body or "").replace("\r\n", "\n").split("\n")
    start = next((i for i, ln in enumerate(lines) if re.match(r"^#{1,3}\s+.*required to deploy", ln, re.I)), None)
    if start is None:
        return None
    rest = lines[start + 1:]
    end = next((i for i, ln in enumerate(rest) if re.match(r"^#{1,2}\s", ln) or re.match(r"^#{3}\s+infrastructure impact", ln, re.I)), len(rest))
    section, prev = "\n".join(rest[:end]), None
    while prev != section:
        prev, section = section, re.sub(r"<!--[\s\S]*?-->", "", section)
    return section.strip()


def words(line):
    """A line as the parser compares it: list marks, code ticks and bold off, one space, no end stop."""
    w = line.strip()
    w = re.sub(r"^([-*+]|\d+[.)])\s+", "", w)
    w = re.sub(r"^\[[xX]\]\s+", "", w)
    w = w.replace("`", "").replace("**", "").replace("__", "")
    return " ".join(w.split()).rstrip(".").strip()


def name_words(w):
    n = w.lower()
    n = re.sub(r"^(the )?deploy ", "", n)
    return re.sub(r" (workflow|action|github action)$", "", n).strip()


def functions_at(text):
    """{fn_key: canonical name} of every function app/functions/index.js registers (addfn_*("light.v2.x", ...))."""
    return {fn_key(n): n.replace(".", "-") for n in re.findall(r"addfn_\w+\(\s*[\"']([^\"']+)[\"']", text or "")}


def template_lines(text):
    """The template's own lines in its deploy section (its prose; YOUR_ANSWER stays the placeholder)."""
    sec = deploy_section(text) or ""
    return {" ".join(x.split()) for x in sec.split("\n") if x.strip() and PLACEHOLDER not in x}


def plan(section, functions, template=()):
    """The plan from the section's text (None: no section) and the functions registered where it landed (None: not
    read). Returns {state, why, unclear, notes, items}; items: [(workflow, part or name or None)] and
    [("manual", file)]."""
    if section is None:
        return {"state": "unavailable", "why": "its pull request has no \"Github Actions Required to Deploy\" section",
                "unclear": [], "notes": [], "items": []}
    template = {" ".join(x.split()) for x in TEMPLATE_LINES} | set(template)
    lines = [ln for ln in section.split("\n") if ln.strip() and " ".join(ln.split()) not in template]
    if any(PLACEHOLDER in ln for ln in lines):
        return {"state": "unavailable", "why": "the section still holds the template's YOUR_ANSWER", "unclear": [], "notes": [], "items": []}
    if not lines:
        return {"state": "unavailable", "why": "the section is empty (an empty section never means no deploy)",
                "unclear": [], "notes": [], "items": []}
    ws = [words(ln) for ln in lines]
    if all(w.lower() in NO_DEPLOY for w in ws):
        return {"state": "none", "why": f"the section says \"{ws[0]}\"", "unclear": [], "notes": [], "items": []}
    unclear, notes, fns, flows, manual = [], [], [], {}, []
    for raw, w in zip(lines, ws):
        toks = [x.strip() for x in w.split(",") if x.strip()]
        if toks and all(re.fullmatch(r"functions:\S+", x) for x in toks):
            for x in toks:
                name = x[len("functions:"):]
                if "=" in name:
                    unclear.append(f"{raw.strip()} (a traffic split is the person's call)")
                elif not re.fullmatch(r"[A-Za-z0-9.-]+", name):
                    unclear.append(f"{raw.strip()} (\"{name}\" is no function name)")
                else:
                    fns.append(name)
            continue
        n = name_words(w)
        if n in WORDS:
            wf, parts = WORDS[n]
            if parts:
                flows[wf] = tuple(sorted(set(flows.get(wf) or ()) | set(parts)))
            else:
                flows[wf] = None
            continue
        if n in MANUAL:
            manual.append(MANUAL[n])
            continue
        if w.lower() in NO_DEPLOY:
            unclear.append(f"{raw.strip()} (\"none\" beside deploys)")
            continue
        unclear.append(raw.strip())
    items, seen = [], set()
    for name in fns:
        k = fn_key(name)
        if k in seen:
            continue
        seen.add(k)
        via = VIA_WORKFLOW.get(k)
        if via and not (via in flows and (flows[via] is None or "functions" in flows[via])):
            unclear.append(f"functions:{name} (docs/pr-conventions.md: it deploys through {WORKFLOWS[VIA_WORKFLOW[k]]['name']}; list that)")
            continue
        if via:
            notes.append(f"{name}: {WORKFLOWS[via]['name']} deploys it")
            continue
        if k in API_FUNCTIONS and "functions" in (flows.get("api") or ()):
            notes.append(f"{name}: Deploy API deploys it")
            continue
        if k in ADMIN_FUNCTIONS and "admin" in flows:
            notes.append(f"{name}: Deploy Admin deploys it")
            continue
        items.append(("function", functions.get(k) if functions and k in functions else name,
                      bool(functions) and k in functions))
    for wf, parts in flows.items():
        for p in parts or (None,):
            items.append((wf, p, True))
    for f in dict.fromkeys(manual):
        items.append(("manual", f, True))
    state = "manual" if unclear or manual or any(not ok for _, _, ok in items) else "ready"
    why = (f"unclear: {'; '.join(unclear)}" if unclear else "it lists a step Herdr never runs" if manual
           else "a function isn't registered where it landed" if state == "manual" else None)
    return {"state": state, "why": why, "unclear": unclear, "notes": notes, "items": items}


def dispatch_inputs(text):
    """The workflow_dispatch inputs a workflow file defines: {name: {required, default, type, options}}, or None when
    it has no workflow_dispatch. A plain indentation reader: enough for these files, and a change it can't read leaves
    the workflow unverified (never dispatched)."""
    lines = (text or "").split("\n")
    i = next((k for k, ln in enumerate(lines) if re.fullmatch(r"\s*workflow_dispatch:\s*", ln)), None)
    if i is None:
        return None
    base = len(lines[i]) - len(lines[i].lstrip())
    out, cur, in_inputs, ind_inputs, ind_name, opts = {}, None, False, None, None, False
    for ln in lines[i + 1:]:
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        ind = len(ln) - len(ln.lstrip())
        if ind <= base:
            break
        s = ln.strip()
        if not in_inputs:
            if s == "inputs:":
                in_inputs, ind_inputs = True, ind
            continue
        if ind <= ind_inputs:
            break
        if ind_name is None or ind == ind_name:
            m = re.fullmatch(r"([A-Za-z0-9_-]+):", s)
            if m:
                ind_name, cur, opts = ind, m.group(1), False
                out[cur] = {"required": False, "default": None, "type": None, "options": []}
                continue
        if cur is None:
            continue
        if opts and s.startswith("- "):
            out[cur]["options"].append(s[2:].strip().strip("'\""))
            continue
        m = re.fullmatch(r"([A-Za-z_]+):\s*(.*)", s)
        if m:
            k, v = m.group(1), m.group(2).strip().strip("'\"")
            opts = k == "options"
            if k == "required":
                out[cur]["required"] = v.lower() == "true"
            elif k in ("default", "type"):
                out[cur][k] = v
    return out


def run_name(text):
    """A workflow's run-name (folded lines joined), or ''."""
    lines = (text or "").split("\n")
    i = next((k for k, ln in enumerate(lines) if ln.startswith("run-name:")), None)
    if i is None:
        return ""
    got = [lines[i][len("run-name:"):]]
    for ln in lines[i + 1:]:
        if ln and not ln[0].isspace():
            break
        got.append(ln)
    return " ".join(" ".join(got).split())


def check_workflow(key, text):
    """None when the workflow on master still matches what Herdr sends and reads; else why not."""
    wf = WORKFLOWS[key]
    inputs = dispatch_inputs(text)
    if inputs is None:
        return f"{wf['file']} has no workflow_dispatch on master"
    for name in list(wf["send"]) + list(wf.get("needs") or ()):
        if name not in inputs and name not in ("force", "skip_eu", "deploy_core_app_hosting"):
            return f"{wf['file']} has no {name} input on master"
    env = inputs.get("environment") or {}
    if env.get("options") and "prod" not in env["options"]:
        return f"{wf['file']}'s environment has no prod on master"
    if "deploy_target" in (wf.get("needs") or ()) and not {"both", "functions", "hosting"} <= set((inputs.get("deploy_target") or {}).get("options") or []):
        return f"{wf['file']}'s deploy_target options changed on master"
    for name, d in inputs.items():
        sent = name in wf["send"] or name in (wf.get("needs") or ())
        if d["required"] and not sent and not d["default"]:
            return f"{wf['file']} now requires {name}, which Herdr doesn't send"
        if (d["type"] == "boolean" and not sent and str(d["default"] or "false").lower() != "false"):
            return f"{wf['file']}'s {name} defaults to {d['default']} on master"
    rn = run_name(text)
    if f"'{wf['tag']}'" not in rn or wf["label"] not in rn:
        return f"{wf['file']}'s run titles changed on master (its runs couldn't be told apart)"
    return None


# ---- runs: which run deployed which target -------------------------------------------------------------------------

def run_covers(key, part, title, file):
    """A run's title on master deploys this target in prod (never another environment, never EU skipped)."""
    wf = WORKFLOWS.get(key)
    title = str(title or "")
    if key == "hosting":
        if file == wf["file"]:
            return title == f"{wf['tag']} Deploy Hosting"
        return any(file == a["file"] and title.startswith(a["prefix"]) for a in ALSO.get("hosting") or [])
    if file != wf["file"]:
        return False
    if key == "function":
        pre = f"{wf['tag']} Deploy "
        if not title.startswith(pre):
            return False
        names = {fn_key(re.sub(r"=\d+$", "", x.strip()[len("functions:"):] if x.strip().startswith("functions:") else x.strip()))
                 for x in title[len(pre):].split(",") if x.strip()}
        return fn_key(part) in names
    if wf.get("parts"):
        pre = f"{wf['tag']} {wf['name']}"
        if not title.startswith(pre):
            return False
        rest = title[len(pre):]
        if key == "rapid":
            rest = rest.replace(" + core app hosting", "")
        got = next((p for p, sfx in PART_SUFFIX.items() if rest == sfx), None)
        return got == "both" or got == part
    return title == f"{wf['tag']} {wf['name']}"


def files_of(key):
    return [WORKFLOWS[key]["file"]] + [a["file"] for a in ALSO.get(key) or []]


def run_view(r, mine):
    return {"id": r.get("id"), "url": r.get("url"), "status": r.get("status"), "conclusion": r.get("conclusion"),
            "head_sha": r.get("head_sha"), "created": ms(r.get("created")), "actor": r.get("actor"), "title": r.get("title"),
            "by": "herdr" if mine else "other"}


def maybe_sent(x):
    """A dispatch GitHub took, or may have taken: its POST failed some way other than GitHub refusing it (HTTP 4xx)."""
    return x.get("ok") or (x.get("error") and not re.search(r"HTTP 4\d\d", str(x["error"])))


def judge(g, d, tgt, landing_sha):
    """A landed target's (status, run, why) from its workflow's runs on master: passed once a run whose head holds the
    landing commit succeeded; else running; else submitted (Herdr's dispatch in the last SUBMIT_WINDOW, its run not on
    GitHub yet, or a POST whose answer was lost); else failed (its latest run); else ready. Herdr's own dispatched run
    is known by its id; any other run counts only by its title (this target, prod) and its head (holding the landing)."""
    key, part = tgt["workflow"], tgt.get("part")
    branch = d["landing"]["branch"]
    mine = {x.get("run_id") for x in d.get("dispatches") or [] if x.get("run_id")}
    runs = []
    for f in files_of(key):
        for r in g.runs(f, branch):
            if run_covers(key, part, r.get("title"), f) and r.get("head_sha") and g.contains(landing_sha, r["head_sha"]):
                runs.append(run_view(r, r.get("id") in mine))
    runs.sort(key=lambda r: r["created"], reverse=True)
    ok = next((r for r in runs if r["status"] == "completed" and r["conclusion"] == "success"), None)
    if ok:
        return "success", ok, None
    going = next((r for r in runs if r["status"] != "completed"), None)
    if going:
        return "running", going, None
    sent = [x for x in d.get("dispatches") or [] if tgt["key"] in (x.get("targets") or []) and maybe_sent(x)]
    last = sent[-1] if sent else None
    if last and now() - int(last["at"]) < SUBMIT_WINDOW and not any(r["id"] == last.get("run_id") for r in runs) \
            and not (runs and runs[0]["created"] >= int(last["at"]) - 30000):
        run = {"id": last["run_id"], "url": last.get("url"), "status": "submitted", "conclusion": None, "by": "herdr"} if last.get("run_id") else None
        return "submitted", run, ("dispatched by Herdr; not passed until its run succeeds" if last.get("ok")
                                  else f"its dispatch's answer was lost ({last['error']}); looking for its run")
    if runs:
        return "failed", runs[0], runs[0]["conclusion"] or "failed"
    return "ready", None, None


def attribute(g, d):
    """A dispatch GitHub gave no run id for: its run is the one run of exactly its title on its ref, by the same person,
    started within SUBMIT_WINDOW after it. Two or none: left unattributed (never a newer, unrelated run)."""
    for x in d.get("dispatches") or []:
        if not maybe_sent(x) or x.get("run_id") or now() - int(x.get("at") or 0) > 24 * 3600000:
            continue
        taken = {y.get("run_id") for y in d.get("dispatches") or [] if y.get("run_id")}
        cands = [r for r in g.runs(x["file"], x["ref"]) if r.get("title") == x.get("title") and r.get("actor") == x.get("by")
                 and r.get("id") not in taken and -30000 <= ms(r.get("created")) - int(x["at"]) <= SUBMIT_WINDOW]
        if len(cands) == 1:
            x["run_id"], x["url"] = cands[0]["id"], cands[0]["url"]
            x["ok"] = True


# ---- landing on master ----------------------------------------------------------------------------------------------

def landing(g, branch, merge_commit, base, merged_at, depth=0):
    """Where the merged change is on master: {state: landed, sha, at, via} or {state: waiting, why, via}."""
    out = {"branch": branch}
    if base == branch:
        return {**out, "state": "landed", "sha": merge_commit, "at": ms(merged_at), "via": None, "why": None}
    if g.contains(merge_commit, branch):
        return {**out, "state": "landed", "sha": merge_commit, "at": None, "via": {"branch": base}, "why": None}
    open_ = None
    for p in g.prs_from(base):
        st = str(p.get("state") or "").upper()
        mc = (p.get("mergeCommit") or {}).get("oid")
        if st == "MERGED" and mc and p.get("headRefOid") and g.contains(merge_commit, p["headRefOid"]):
            via = {"pr": p.get("number"), "url": p.get("url"), "branch": base, "into": p.get("baseRefName"), "state": "merged"}
            if p.get("baseRefName") == branch:
                return {**out, "state": "landed", "sha": mc, "at": ms(p.get("mergedAt")), "via": via, "why": None}
            if depth + 1 < STACK_DEPTH:
                # merged into another branch in turn: where that merge went
                below = landing(g, branch, mc, p.get("baseRefName"), p.get("mergedAt"), depth + 1)
                below["chain"] = [via] + (below.get("chain") or [])
                return below
        if st == "OPEN" and open_ is None:
            open_ = {"pr": p.get("number"), "url": p.get("url"), "branch": base, "into": p.get("baseRefName"), "state": "open"}
    if open_:
        return {**out, "state": "waiting", "sha": None, "at": None, "via": open_,
                "why": f"merged into {base}; waits for #{open_['pr']} ({base} → {open_['into']}) to merge"}
    return {**out, "state": "waiting", "sha": None, "at": None, "via": {"branch": base},
            "why": f"merged into {base}, not {branch}; nothing merges {base} into {branch} yet"}


# ---- the pass -------------------------------------------------------------------------------------------------------

def await_deploy(s, t, merge_commit=None, merged_at=None):
    """The task's pull request merged (review_pass(), or merged at its Accept): it moves to Awaiting deploy. Its
    worktree goes now when it can (finish()), as the merge used to take it; the tick reads the rest."""
    from .github import finish
    a = t.get("awaiting") or {}
    h = t.get("handoff") or {}
    repo, pr = a.get("repo") or h.get("repo"), a.get("pr") or h.get("pr")
    finish(s, t, even_if_busy=True)
    t["state"] = DEPLOYING
    t.pop("done_at", None)
    t["merged"] = True
    t["deploy"] = {"state": "unknown", "text": TEXT["unknown"], "repo": repo, "pr": pr, "url": a.get("url") or h.get("url"),
                   "merge_commit": merge_commit, "merged_at": merged_at, "since": now(), "base": None, "landing": None,
                   "plan": None, "targets": [], "dispatches": [], "fails": 0, "next": 0}
    s.dirty = True


def due(t):
    return deploying(t) and now() >= int((t.get("deploy") or {}).get("next") or 0)


def target_key(wf, part):
    return wf if part is None else f"{wf}:{part}"


def refresh(d, g, force_plan=False):
    """Reads everything again from GitHub into d (the deploy record). Raises GitHubError."""
    cfg = config_for(d["repo"])
    pr = g.pr(d["pr"])
    if str(pr.get("state") or "").upper() != "MERGED":
        raise GitHubError(f"pull request #{d['pr']} isn't merged on GitHub")
    d["merge_commit"] = (pr.get("mergeCommit") or {}).get("oid") or d.get("merge_commit")
    d["merged_at"] = pr.get("mergedAt") or d.get("merged_at")
    d["base"], d["url"] = pr.get("baseRefName"), pr.get("url") or d.get("url")
    if not d["merge_commit"]:
        raise GitHubError("GitHub gave no merge commit")
    d["landing"] = landing(g, cfg["branch"], d["merge_commit"], d["base"], d["merged_at"])
    landed = d["landing"]["state"] == "landed"
    section = deploy_section(pr.get("body"))
    fref = d["landing"]["sha"] if landed else d["merge_commit"]
    h = hashlib.sha1(json.dumps([section, fref]).encode()).hexdigest()[:16]
    old = d.get("plan") or {}
    if force_plan or old.get("hash") != h or now() - int(old.get("read_at") or 0) > PLAN_EVERY:
        build_plan(d, g, section, fref, h)
    if landed:
        attribute(g, d)
        for tgt in d["targets"]:
            if tgt["status"] in ("manual", "unverified"):
                continue
            if tgt["status"] == "success" and tgt.get("landing") == d["landing"]["sha"]:
                continue  # passed for this landing: it stays passed (its run may since be off GitHub's first page)
            tgt["status"], tgt["run"], tgt["why"] = judge(g, d, tgt, d["landing"]["sha"])
            tgt["landing"] = d["landing"]["sha"] if tgt["status"] == "success" else None
    else:
        for tgt in d["targets"]:
            if tgt["status"] not in ("manual", "unverified"):
                tgt["status"], tgt["run"], tgt["why"] = "waiting", None, d["landing"]["why"]
    d["state"] = overall(d)
    d["text"] = TEXT[d["state"]]


def build_plan(d, g, section, fref, h):
    cfg = config_for(d["repo"])
    tpl = template_lines(g.file(cfg["template"], cfg["branch"]))
    p = plan(section, {}, tpl)
    if any(wf == "function" for wf, _, _ in p["items"]):
        p = plan(section, functions_at(g.file(cfg["index"], fref)), tpl)
    bad = {}
    for wf in {wf for wf, _, _ in p["items"] if wf in WORKFLOWS}:
        bad[wf] = check_workflow(wf, g.file(f".github/workflows/{WORKFLOWS[wf]['file']}", cfg["branch"]))
    keep = {x["key"]: x for x in d.get("targets") or []}
    targets = []
    for wf, part, ok in p["items"]:
        if wf == "manual":
            key = f"manual:{part}"
            targets.append({"key": key, "workflow": "manual", "file": part, "label": part.rsplit(".", 1)[0].replace("-", " ").capitalize(),
                            "environment": None, "ref": cfg["branch"], "status": "manual", "why": "Herdr never runs it: by hand on GitHub",
                            "run": None, "workflow_url": workflow_url(d["repo"], part)})
            continue
        w = WORKFLOWS[wf]
        key = f"function:{part}" if wf == "function" else target_key(wf, part)
        label = part if wf == "function" else w["name"] + (f" ({part})" if part else "")
        status, why = ("unverified", f"{part} isn't registered in {cfg['index']} at {fref[:7]}") if not ok else \
            ("unverified", bad[wf]) if bad.get(wf) else ((keep.get(key) or {}).get("status") or "ready", None)
        if status in ("manual", "unverified"):
            status = status if not ok or bad.get(wf) else "ready"
        targets.append({"key": key, "workflow": wf, "part": part, "file": w["file"], "label": label, "environment": cfg["environment"],
                        "ref": cfg["branch"], "status": status, "why": why, "run": (keep.get(key) or {}).get("run"),
                        "landing": (keep.get(key) or {}).get("landing"), "workflow_url": workflow_url(d["repo"], w["file"])})
    if any(x["status"] == "unverified" for x in targets) and p["state"] == "ready":
        p["state"], p["why"] = "manual", next(x["why"] for x in targets if x["status"] == "unverified")
    d["targets"] = targets
    d["plan"] = {"state": p["state"], "why": p["why"], "section": (section or "")[:2000] if section is not None else None,
                 "unclear": p["unclear"], "notes": p["notes"], "hash": h, "ref": fref, "read_at": now()}


def overall(d):
    p = (d.get("plan") or {}).get("state")
    if p == "none":
        return "none"
    if p == "unavailable":
        return "unavailable"
    if (d.get("landing") or {}).get("state") != "landed":
        return "waiting"
    st = [x["status"] for x in d.get("targets") or []]
    if any(x in ("running", "submitted") for x in st):
        return "running"
    if "failed" in st:
        return "failed"
    if p == "manual" or any(x in ("manual", "unverified") for x in st):
        return "manual"
    if st and all(x == "success" for x in st):
        return "success"
    return "ready"


def every(state):
    return {"waiting": WAIT_EVERY, "running": RUNNING_EVERY, "manual": MANUAL_EVERY, "unavailable": MANUAL_EVERY}.get(state, READY_EVERY)


def deploy_pass(s, t, report, force=False):
    """One read of an Awaiting deploy task when due (or force): its record refreshed; every deploy verified, or an
    explicit no-deploy plan: done. GitHub out of reach: tried again later and later, "GitHub unavailable" once that
    lasts (its last known state kept as was)."""
    if not (force or due(t)):
        return
    d = t["deploy"]
    d["checked"] = now()
    s.dirty = True
    before = d.get("state")
    g = GitHub(d["repo"], d.setdefault("seen", {}))
    try:
        refresh(d, g)
    except GitHubError as e:
        d["fails"] = int(d.get("fails") or 0) + 1
        d["error"] = str(e)
        d["next"] = now() + min(RETRY_MAX, RETRY_FIRST * 2 ** (d["fails"] - 1))
        if d["fails"] >= UNAVAILABLE_AFTER and d.get("text") != TEXT["github"]:
            if d.get("state") not in (None, "unknown"):
                d["was"] = d["state"]
            d["text"] = TEXT["github"]
            report.setdefault("deploy_changed", []).append({"ref": s.ref(t), "deploy": "unavailable"})
        return
    if len(d["seen"]) > 400:
        d["seen"] = dict(list(d["seen"].items())[-200:])
    d.update(fails=0, error=None, ok_at=now(), next=now() + every(d["state"]))
    d.pop("was", None)
    if d["state"] in ("success", "none"):
        t["state"], t["done_at"] = "done", now()
        s.dirty = True
        report["done"].append(t["id"])
        return
    if before != d["state"]:
        report.setdefault("deploy_changed", []).append({"ref": s.ref(t), "deploy": d["state"]})


# ---- the person's dispatch ------------------------------------------------------------------------------------------

def units(d, only=None):
    """What a dispatch would send now: one unit per workflow, its targets ready or failed (a retry): [{workflow, file,
    name, ref, environment, inputs, title, targets, url}]."""
    out = {}
    for x in d.get("targets") or []:
        if x["workflow"] not in WORKFLOWS or x["status"] not in ("ready", "failed"):
            continue
        if only and x["key"] not in only and x["workflow"] not in only:
            continue
        out.setdefault(x["workflow"], []).append(x)
    res = []
    for wf, xs in out.items():
        w = WORKFLOWS[wf]
        inputs = dict(w["send"])
        if wf == "function":
            inputs["deploy_only"] = ",".join(f"functions:{x['part']}" for x in xs)
            title = f"{w['tag']} Deploy {inputs['deploy_only']}"
        elif w.get("parts"):
            parts = {x["part"] for x in xs}
            inputs["deploy_target"] = "both" if parts >= {"functions", "hosting"} else parts.pop()
            title = f"{w['tag']} {w['name']}{PART_SUFFIX[inputs['deploy_target']]}"
        else:
            title = f"{w['tag']} {w['name']}"
        res.append({"workflow": wf, "file": w["file"], "name": w["name"], "ref": xs[0]["ref"], "environment": inputs["environment"],
                    "inputs": inputs, "title": title, "targets": [x["key"] for x in xs], "url": workflow_url(d["repo"], w["file"])})
    return res


def blocked(d):
    """Why nothing may be dispatched now (None: it may)."""
    if (d.get("landing") or {}).get("state") != "landed":
        return (d.get("landing") or {}).get("why") or "not on master yet"
    p = d.get("plan") or {}
    if p.get("state") != "ready":
        return p.get("why") or TEXT.get(p.get("state"), "the plan isn't clear")
    if any(x["status"] in ("manual", "unverified") for x in d.get("targets") or []):
        return "a target needs a manual step or couldn't be verified"
    if d.get("text") == TEXT["github"]:
        return "GitHub unavailable"
    return None


def preview(d, only=None):
    """The authoritative view of what `deploy --yes` would do: {enabled, why, units, fallback}."""
    why = blocked(d)
    us = units(d, only) if not why else []
    if not why and not us:
        why = "nothing to dispatch: every target ran, runs or waits" if d.get("targets") else "nothing to dispatch"
    fallback = [{"label": x["label"], "url": x["workflow_url"], "status": x["status"]} for x in d.get("targets") or []
                if x["status"] not in ("success",)]
    return {"enabled": not why, "why": why, "units": us, "fallback": fallback}


def dispatch(s, t, only=None):
    """The person's dispatch (`deploy --yes`): everything read again from GitHub first (the plan's section, the landing,
    master's head holding it, each workflow's definition on master, its runs), then one workflow_dispatch per unit.
    Returns (sent units, why none). A failed POST is kept and told; nothing is retried by itself."""
    d = t["deploy"]
    g = GitHub(d["repo"], d.setdefault("seen", {}))
    try:
        refresh(d, g, force_plan=True)
        d.update(fails=0, error=None, ok_at=now())
        d.pop("was", None)
    except GitHubError as e:
        s.dirty = True
        return [], f"GitHub couldn't be read again: {e}"
    s.dirty = True
    v = preview(d, only)
    if not v["enabled"]:
        return [], v["why"]
    cfg = config_for(d["repo"])
    try:
        ref_sha = g.head(cfg["branch"])
        if not g.contains(d["landing"]["sha"], ref_sha):
            return [], f"{cfg['branch']}'s head {ref_sha[:7]} doesn't hold {d['landing']['sha'][:7]}"
        who = g.login()
    except GitHubError as e:
        return [], f"GitHub couldn't be read again: {e}"
    sent = []
    for u in v["units"]:
        rec = {"at": now(), "workflow": u["workflow"], "file": u["file"], "ref": u["ref"], "ref_sha": ref_sha, "inputs": u["inputs"],
               "title": u["title"], "targets": u["targets"], "by": who, "run_id": None, "url": None, "ok": False, "error": None}
        d.setdefault("dispatches", []).append(rec)
        try:
            rec["run_id"], rec["url"] = g.dispatch(u["file"], u["ref"], u["inputs"])
            rec["ok"] = True
        except GitHubError as e:
            rec["error"] = str(e)
        for x in d["targets"]:
            if x["key"] in u["targets"] and rec["ok"]:
                x["status"], x["why"] = "submitted", "dispatched by Herdr; not passed until its run succeeds"
                x["run"] = {"id": rec["run_id"], "url": rec["url"], "status": "submitted", "conclusion": None, "by": "herdr"} if rec["run_id"] else None
        sent.append(rec)
    d["dispatches"] = d["dispatches"][-40:]
    d["state"], d["text"] = overall(d), TEXT[overall(d)]
    d["next"] = now() + RUNNING_EVERY
    return sent, None


# ---- words ----------------------------------------------------------------------------------------------------------

def short(d):
    """A row's words: "PR #19756 · Deploying · 2 of 5 passed"."""
    d = d or {}
    bits = [f"PR #{d['pr']}"] if d.get("pr") else []
    text = d.get("text") or TEXT["unknown"]
    if d.get("text") == TEXT["github"] and d.get("was"):
        text += f" (was {TEXT.get(d['was'], d['was']).lower()})"
    bits.append(text)
    ts = [x for x in d.get("targets") or [] if x["workflow"] != "manual"]
    if ts and (d.get("landing") or {}).get("state") == "landed":
        bits.append(f"{sum(1 for x in ts if x['status'] == 'success')} of {len(ts)} passed")
    elif (d.get("landing") or {}).get("via", {}) and (d["landing"].get("via") or {}).get("pr") and d["landing"]["state"] == "waiting":
        bits.append(f"after #{d['landing']['via']['pr']}")
    return " · ".join(bits)


def lines(d):
    """show's and deploy's lines: the landing, the plan, each target with its run."""
    d = d or {}
    out = [f"deploy: {d.get('text') or TEXT['unknown']}" + (f" (was {TEXT.get(d.get('was'), d.get('was'))})" if d.get("was") else "")
           + (f" · pull request #{d['pr']} {d.get('url') or ''}".rstrip() if d.get("pr") else "")]
    ln = d.get("landing") or {}
    if ln.get("state") == "landed":
        via = ln.get("via") or {}
        out.append(f"  landed on {ln['branch']} at {str(ln.get('sha'))[:7]}" + (f" through #{via['pr']} ({via['branch']} → {via.get('into')})" if via.get("pr")
                                                                               else f" (from {via['branch']})" if via.get("branch") else ""))
    elif ln.get("state") == "waiting":
        out.append(f"  waiting for {ln['branch']}: {ln.get('why')}" + (f" {ln['via']['url']}" if (ln.get("via") or {}).get("url") else ""))
    p = d.get("plan") or {}
    if p:
        out.append(f"  plan: {p.get('state')}" + (f" ({p['why']})" if p.get("why") else "") + (f", read at {str(p.get('ref'))[:7]}" if p.get("ref") else ""))
        for n in p.get("notes") or []:
            out.append(f"    note: {n}")
    for x in d.get("targets") or []:
        r = x.get("run") or {}
        bit = f"  - {x['label']}: {TARGET_TEXT.get(x['status'], x['status'])}"
        if x.get("environment"):
            bit += f" · {x['workflow'] if x['workflow'] != 'function' else 'Deploy Function'} on {x['ref']} to {x['environment']}"
        if r.get("url"):
            bit += f" · run {r['url']}" + (" (not Herdr's)" if r.get("by") == "other" else "")
        elif x["status"] not in ("success",):
            bit += f" · {x['workflow_url']}"
        if x.get("why") and x["status"] not in ("success", "running"):
            bit += f" ({x['why']})"
        out.append(bit)
    if d.get("error"):
        out.append(f"  last read failed ({d.get('fails')}x): {d['error']}")
    return out

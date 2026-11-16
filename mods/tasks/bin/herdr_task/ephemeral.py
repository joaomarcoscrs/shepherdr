"""SHEP-4: a Roboflow task's ephemeral environment, as its Accept card shows it beside the pull request.

Roboflow's featur controller turns a roboflow/roboflow pull request labelled `ephemeral` (or `dev-ephemeral`) into a
live copy of the platform, and reports it on the PR's head commit as the check run "featur / environment" from the
roboflow-featur app: its title the phase ("Ready — pr-<n>.eph.roboflow.one", "Building — …", "Environment
destroyed"), its details_url the environment, its summary the lease ("| Expires | <iso> (in …) |"). That check is
the one source read here, read-only: nothing is labelled, built or torn down from this, and no link is made up. Only
a check that says Ready on the pull request's current head, on an open pull request still labelled for it, with a
link to that same pull request's own host and a lease not yet over, is "ready" with a link; everything else says
where it stands, with at most a link to the check's own page on GitHub."""

import calendar
import json
import re
import time

from .github import gh

# The repos whose pull requests get an ephemeral environment (featur builds roboflow/roboflow only).
REPOS = {"roboflow/roboflow"}
CHECK = "featur / environment"
APP = "roboflow-featur"
LABELS = {"ephemeral", "dev-ephemeral"}
FAILED = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
EXPIRES = re.compile(r"\|\s*Expires\s*\|\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")


def applies(t):
    """A task whose pull request is in a repo featur serves: its Accept card shows the environment."""
    pr = t.get("pr") or {}
    return bool(pr.get("number") and str(pr.get("repo") or "").lower() in REPOS)


def host_ok(url, number):
    """The environment's own link for this pull request (pr-<n>.eph.roboflow.one, or the dev controller's
    pr-<n>.dev.eph.roboflow.one), over https: never another PR's or another site's, whatever a check says."""
    return bool(re.fullmatch(rf"https://pr-{int(number)}\.(?:dev\.)?eph\.roboflow\.one/?", str(url or "")))


def expires_of(summary):
    """The lease's end (epoch ms) the check's summary gives, or None."""
    m = EXPIRES.search(summary or "")
    return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ")) * 1000 if m else None


def phase_of(title):
    """The check title's phase: "Building — pr-1.eph.roboflow.one" -> "Building"."""
    return re.split(r"\s+[—–-]\s+", str(title or "").strip(), maxsplit=1)[0].strip() or None


def latest(runs):
    """The newest featur check run among a commit's (a rerun replaces an older one)."""
    mine = [c for c in runs if c.get("name") == CHECK and ((c.get("app") or {}).get("slug") == APP)]
    return max(mine, key=lambda c: (c.get("started_at") or "", c.get("id") or 0)) if mine else None


def read(pr, run, at):
    """Where the environment stands, from the pull request (GitHub's pulls API answer) and its head's featur check run
    (or None): {state, phase?, url?, host?, expires?, check_url?}. state: ready (live, url set), starting, failed,
    gone (torn down, or going), none (never asked for)."""
    number = pr.get("number")
    labelled = bool(LABELS & {(x.get("name") or "") for x in pr.get("labels") or []})
    out = {"state": "none"}
    if run:
        out["phase"] = phase_of((run.get("output") or {}).get("title"))
        if str(run.get("html_url") or "").startswith("https://github.com/"):
            out["check_url"] = run["html_url"]
    if pr.get("state") != "open" or pr.get("merged_at"):
        # featur tears it down when its pull request closes or merges
        if run or labelled:
            out.update(state="gone", why="closed")
        return out
    if not run:
        if labelled:
            out.update(state="starting", phase=None)
        return out
    status, conclusion = (run.get("status") or "").lower(), (run.get("conclusion") or "").lower()
    if status != "completed":
        out["state"] = "starting"
    elif conclusion in FAILED:
        out["state"] = "failed"
    elif conclusion != "success" or (out.get("phase") or "").lower() != "ready":
        out["state"] = "gone"
    elif not labelled:
        # its label taken off: being torn down, whatever the check still says
        out.update(state="gone", why="unlabelled")
    else:
        url = run.get("details_url")
        expires = expires_of((run.get("output") or {}).get("summary"))
        if not host_ok(url, number):
            out.update(state="unknown", why="link")
        elif expires and expires <= at:
            out.update(state="gone", why="expired", expires=expires)
        else:
            out.update(state="ready", url=url.rstrip("/"), host=url.split("//", 1)[1].rstrip("/"))
            if expires:
                out["expires"] = expires
    return out


def ephemeral_of(t, at=None):
    """A Roboflow task's environment as its Accept card shows it (read()), or None for any other task. GitHub not
    answering: {state: unknown}, never a guess."""
    if not applies(t):
        return None
    repo, number = t["pr"]["repo"], int(t["pr"]["number"])
    ok, out, _ = gh("api", f"repos/{repo}/pulls/{number}")
    try:
        pr = json.loads(out) if ok else None
    except ValueError:
        pr = None
    sha = ((pr or {}).get("head") or {}).get("sha") if isinstance(pr, dict) else None
    if not sha:
        return {"state": "unknown"}
    ok, out, _ = gh("api", f"repos/{repo}/commits/{sha}/check-runs?check_name={CHECK.replace(' ', '%20').replace('/', '%2F')}")
    try:
        runs = json.loads(out).get("check_runs") if ok else None
    except (ValueError, AttributeError):
        runs = None
    if runs is None:
        return {"state": "unknown"}
    return read(pr, latest(runs), int(time.time() * 1000) if at is None else at)

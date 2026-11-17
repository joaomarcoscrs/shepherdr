"""SHEP-10: a branch update. The person names an existing pull request and asks for its branch to be brought up to date
with its base ("update featur #280 with master"): `herdr-task add … --update-pr 280 [--rebase]`. Still a task (a worker
does it, in the folder itself: no worktree, branch or pull request of Herdr's own), but nothing for Herdr to merge or hand
off: the push to that pull request's own branch is the delivery, and the pull request stays the person's.

The person's ask is their Accept given ahead for that one task (SHEP-3's per-task auto-accept, by "branch-update"), with
Herdr's own checks read on GitHub, never taken from the agent's word: ready waits while its checks still run, and the
tick accepts it by itself only when it is clean: the pull request still open on the same branch and base, its head
containing the base as it was when the person asked, pushed with no force (the head it had then is in its history), no
conflicts and every check passed. Anything else (a conflict, a failing check, a rewritten history, the branch moved
since ready) stays Ready for the person's own Accept, saying why. The space's delivery profile, Merge and Accept when
ready settings are never read or changed for it: an Open PR space's branch updates too, since there is no new pull
request to hand off. Herdr never merges the pull request, pushes, or force-pushes anything."""

import json
import re

from .base import Fail, git, now
from .github import gh, latest_checks, pr_pending, remote_of, BAD

HOW = ("merge", "rebase")
BY = "branch-update"  # auto_accept.by: the person's ask for this update was its Accept given ahead
PR_REF = re.compile(r"^(?:#?(\d+)|https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)(?:[/?#].*)?)$")
FIELDS = "number,state,url,headRefName,baseRefName,headRefOid,isCrossRepository,mergeable,statusCheckRollup"


def parse_pr(value):
    """--update-pr's words: (number, "owner/name" or None). 280, #280 or the pull request's link."""
    m = PR_REF.match(str(value or "").strip())
    if not m:
        raise Fail(f"--update-pr takes the pull request's number or link (280, #280, https://github.com/o/r/pull/280), "
                   f"not {value!r}.")
    return int(m.group(1) or m.group(3)), m.group(2)


def repo_of(folder):
    ok, out, err = gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner", cwd=folder)
    if not (ok and out):
        raise Fail(f"Herdr couldn't read this repo on GitHub ({err or 'gh failed'}); a branch update needs it.")
    return out


def read_pr(repo, number, cwd=None):
    """The pull request as GitHub has it now (FIELDS, its checks each by their newest run), or None when GitHub couldn't
    be asked."""
    ok, out, _ = gh("pr", "view", str(int(number)), "--repo", repo, "--json", FIELDS, cwd=cwd)
    try:
        v = json.loads(out) if ok else None
    except ValueError:
        return None
    if not isinstance(v, dict):
        return None
    v["statusCheckRollup"] = latest_checks(v.get("statusCheckRollup") or [])
    return v


def base_sha(folder, repo, base):
    """The base branch's commit on GitHub now (what the person asked to bring in), or None."""
    ok, out, _ = gh("api", f"repos/{repo}/branches/{base}", "--jq", ".commit.sha", cwd=folder)
    return out.strip() if ok and re.fullmatch(r"[0-9a-f]{40}", out.strip()) else None


def plan(s, value, how):
    """add --update-pr: the pull request read on GitHub and kept on the task (t["branch_update"]). Refused unless it is
    an open pull request of this repo whose branch can take a normal push from here."""
    if s.loc["kind"] != "repo":
        raise Fail("A branch update needs a git repo; this folder isn't one.")
    how = how or "merge"
    if how not in HOW:
        raise Fail(f"A branch update is a merge or a rebase, not {how!r}.")
    number, named = parse_pr(value)
    folder = s.loc["folder"]
    repo = repo_of(folder)
    if named and named.lower() != repo.lower():
        raise Fail(f"Pull request {value} is in {named}, but this folder is {repo}: add it in that repo's folder.")
    v = read_pr(repo, number, cwd=folder)
    if v is None:
        raise Fail(f"Herdr couldn't read pull request #{number} of {repo} on GitHub just now; try again.")
    if v.get("state") != "OPEN":
        raise Fail(f"Pull request #{number} is {str(v.get('state') or 'not open').lower()}: only an open one's branch is updated.")
    if v.get("isCrossRepository"):
        raise Fail(f"Pull request #{number}'s branch is in a fork: it can't be updated with a normal push from here.")
    base = v.get("baseRefName")
    at = base_sha(folder, repo, base)
    if not at:
        raise Fail(f"Herdr couldn't read {base}'s head on GitHub just now; try again.")
    return {"pr": number, "repo": repo, "url": v.get("url"), "branch": v.get("headRefName"), "base": base, "how": how,
            "from": v.get("headRefOid"), "base_at": at, "at": now()}


def words(bu):
    """The update in a few words: "pull request #280 (joao/x), master merged in"."""
    return (f"pull request #{bu['pr']} ({bu['branch']}), {bu['base']} "
            + ("merged in" if bu.get("how") != "rebase" else "rebased onto"))


def brief_line(bu):
    """The Settings block's Branch update line: what to do, how, and what Herdr checks before it accepts it by itself."""
    n, br, base, url = bu["pr"], bu["branch"], bu["base"], bu.get("url") or f"#{bu['pr']}"
    how = (f"`git merge origin/{base}` into `{br}` (a merge, never a rebase: the branch is already pushed)"
           if bu.get("how") != "rebase" else
           f"rebase `{br}` onto `origin/{base}`, as the person asked. A rebase rewrites a pushed branch and needs a "
           f"force push, which a branch update never does by itself: once rebased and checked, ask the person in your "
           f"chat (question tool) before any `--force-with-lease`; without their yes, push nothing")
    return (f"update {url}'s own branch `{br}` with `{base}` (at least {bu['base_at'][:7]}, its head when the person asked), "
            f"which the person asked for by name. How: fetch, then {how}. Work on `{br}` itself: in the folder when it is "
            f"checked out there (no uncommitted changes of the person's: if there are, stop and ask), else in a temporary "
            f"`git worktree add` outside the repo, removed when you're done; never switch the person's checkout, and touch "
            f"no other branch or pull request. Resolve conflicts keeping both sides' intent; one you can't resolve safely: "
            f"push nothing, abort, and ask. Run the repo's checks (its AGENTS.md); a failure the update caused gets a small "
            f"follow-up commit. Push with a normal `git push`, never force. Then wait for the pull request's checks "
            f"(`gh pr checks {n} --watch`) and run ready (it refuses while they run), saying in your summary what came in, "
            f"any conflicts and how you resolved them, and the checks' result. Herdr reads GitHub itself at ready: when the "
            f"branch contains that base, was pushed without force, has no conflicts and every check passed, it is accepted "
            f"by itself (the person's ask was their Accept); anything else waits for the person, saying why. Never merge "
            f"the pull request, mark it ready for review, edit its description or request reviewers")


def ancestor(folder, a, b):
    return bool(a and b) and git(folder, "merge-base", "--is-ancestor", a, b)[0]


def check(s, t):
    """ready's half for a branch update: the pull request read on GitHub and its branch fetched here. Returns why ready is
    refused for now (its checks still run, GitHub out of reach, its push not made yet), or None with the verdict kept on
    the task (branch_update.check: {head, clean, why, line, at})."""
    bu = t["branch_update"]
    folder, n, ref = s.loc["folder"], bu["pr"], s.ref(t)
    v = read_pr(bu["repo"], n, cwd=folder)
    if v is None:
        return f"Couldn't read pull request #{n} on GitHub just now. Run ready again in a moment."
    head, problems = v.get("headRefOid"), []
    state = str(v.get("state") or "").upper()
    if state != "OPEN":
        problems.append(f"pull request #{n} was {state.lower() or 'closed'} meanwhile")
    if v.get("headRefName") != bu["branch"] or v.get("baseRefName") != bu["base"]:
        problems.append(f"pull request #{n} is now {v.get('headRefName')} into {v.get('baseRefName')}, not {bu['branch']} "
                        f"into {bu['base']}")
    remote = remote_of(folder)
    if not git(folder, "fetch", "--quiet", remote, f"+refs/heads/{bu['branch']}:refs/remotes/{remote}/{bu['branch']}",
               f"+refs/heads/{bu['base']}:refs/remotes/{remote}/{bu['base']}")[0] and state == "OPEN":
        return f"Couldn't fetch {bu['branch']} from {remote} just now. Run ready again in a moment."
    if state == "OPEN":
        fetched = git(folder, "rev-parse", f"refs/remotes/{remote}/{bu['branch']}")[1]
        if fetched != head:
            return (f"GitHub says pull request #{n}'s head is {str(head)[:7]}, but {remote}/{bu['branch']} is "
                    f"{str(fetched)[:7]} here. Run ready again in a moment.")
        local = git(folder, "rev-parse", "--verify", "--quiet", f"refs/heads/{bu['branch']}")[1]
        if local and local != head and ancestor(folder, head, local):
            return (f"{bu['branch']} here is at {local[:7]}, ahead of pull request #{n} ({head[:7]}): push it (a normal "
                    f"`git push`, never force), wait for its checks, then run ready again.")
        if v.get("mergeable") == "UNKNOWN":
            return f"GitHub is still working out whether pull request #{n} merges cleanly. Run ready again in a minute."
        if pr_pending(v):
            return (f"Pull request #{n}'s checks still run at {head[:7]}: wait for them (`gh pr checks {n} --watch`), "
                    "then run ready again.")
        if not ancestor(folder, bu["base_at"], head):
            problems.append(f"its head {head[:7]} doesn't contain {bu['base']} at {bu['base_at'][:7]} (asked for)")
        if bu.get("from") and not ancestor(folder, bu["from"], head):
            problems.append(f"its history was rewritten since {bu['from'][:7]} (a force push)")
        if v.get("mergeable") == "CONFLICTING":
            problems.append(f"GitHub says it conflicts with {bu['base']}")
        bad = [c.get("name") or c.get("context") or "a check" for c in v.get("statusCheckRollup") or []
               if (c.get("conclusion") or c.get("state") or "").upper() in BAD]
        if bad:
            problems.append("checks failing: " + ", ".join(bad[:3]) + (" and more" if len(bad) > 3 else ""))
    checks = v.get("statusCheckRollup") or []
    clean = not problems
    line = (f"pull request #{n} at {str(head)[:7]} contains {bu['base']} {bu['base_at'][:7]}, pushed without force, "
            f"no conflicts, " + (f"{len(checks)} check{'s' if len(checks) != 1 else ''} passed" if checks else "no checks")
            if clean else f"pull request #{n}: " + "; ".join(problems))
    bu["check"] = {"head": head, "clean": clean, "why": None if clean else "; ".join(problems), "line": line, "at": now()}
    s.dirty = True
    return None


def accept_block(s, t):
    """auto-accept's half (tick): why this branch update isn't accepted by itself (it waits for the person's Accept), "" to
    try again at the next tick (GitHub out of reach), or None to accept it: clean at ready, and its pull request still
    open at the head ready checked."""
    bu = t["branch_update"]
    c = bu.get("check") or {}
    if not c or (c.get("at") or 0) < (t.get("ready_at") or 1) - 60000:
        return "Herdr hasn't checked the pull request since it said ready"
    if not c.get("clean"):
        return c.get("why") or "its update isn't clean"
    v = read_pr(bu["repo"], bu["pr"])
    if v is None:
        return ""
    if v.get("state") != "OPEN":
        return f"pull request #{bu['pr']} was {str(v.get('state') or 'closed').lower()} since it said ready"
    if v.get("headRefOid") != c.get("head"):
        return f"pull request #{bu['pr']} moved to {str(v.get('headRefOid'))[:7]} since it was checked at {c['head'][:7]}"
    return None


def guard(t, args):
    """A branch update works in the folder itself on the pull request's branch: never a worktree, branch or base of its own."""
    if not t.get("branch_update"):
        return
    if getattr(args, "worktree", False) or getattr(args, "branch", None) or getattr(args, "base", None):
        raise Fail("A branch update works on its pull request's own branch, in the folder itself: no --worktree, "
                   "--branch or --base.")

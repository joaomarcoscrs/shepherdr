"""SHEP-7: Open PR delivery. A task whose delivery profile is Open PR (settings.py delivery_of()) is never merged by
Herdr: its ready opens its pull request, assigns it as the profile says (added, nobody taken off), waits for the
profile's approval of the pull request's exact head, and hands it to the person, who takes it from there.

The approval is read on GitHub here, never taken from the agent's word: for "jarbas-local", the comment the installed
jarbas-review-local skill publishes once per head (scripts/publish-local-review.js): by the account gh is signed in
with here, carrying `<!-- brains:jarbas-local:<repo>#<n>:<full head sha> -->`, and starting `[Jarbas Local João] —
**APPROVED**`. Passing checks, an approval of an older head, the LGTJarb label (it stays on across heads) or a verdict
other than APPROVED are never one. Herdr itself publishes nothing, requests no reviewer and messages nobody."""

import json
import re

from .base import Fail, git, now
from .github import existing_pr_repo, existing_pr_target, gh, matches_existing_pr, remote_of

GATES = ("jarbas-local",)
GATE_NAMES = {"jarbas-local": "Jarbas Local", "none": "No approval asked"}
# Ready refused this many times with no verdict at all for its head: the agent is told to hand it off as blocked.
MAX_TRIES = 3
VERDICT = re.compile(r"^\s*\[Jarbas Local João\]\s*[—–-]\s*\*\*([A-Z][A-Z _]*)\*\*")
NO_MERGE = "Its delivery is Open PR: Herdr never merges it. The person takes the pull request from there."

_login = {}


def me():
    """The GitHub account gh is signed in with here (who the jarbas-review-local skill publishes as), or None."""
    if "v" not in _login:
        ok, out, _ = gh("api", "user", "--jq", ".login")
        _login["v"] = out.strip() if ok and out.strip() else None
    return _login["v"]


def pr_facts(repo, number, target=None):
    """The pull request now: {state: open|closed|merged, head, assignees, url}, or None when GitHub couldn't be asked."""
    ok, out, _ = gh("api", f"repos/{repo}/pulls/{int(number)}")
    try:
        v = json.loads(out) if ok else None
    except ValueError:
        v = None
    if not isinstance(v, dict):
        return None
    if target and not matches_existing_pr(repo, v, target):
        return None
    state = "merged" if v.get("merged_at") or v.get("merged") else str(v.get("state") or "").lower()
    return {"state": state, "head": (v.get("head") or {}).get("sha"), "url": v.get("html_url"),
            "assignees": [a.get("login") for a in v.get("assignees") or [] if a.get("login")]}


def jarbas_local(repo, number, head):
    """Jarbas Local's verdict for that exact head: {verdict, url} (verdict APPROVED, REQUEST_CHANGES, NEEDS_DISCUSSION or
    BLOCKED), {} when none is published for it, None when GitHub couldn't be asked. Only a comment by the account gh is
    signed in with here counts, the first for that head (the skill publishes one per head and reuses it)."""
    login = me()
    if not login:
        return None
    marker = f"<!-- brains:jarbas-local:{repo}#{int(number)}:{str(head).lower()} -->"
    ok, out, _ = gh("api", "--paginate", f"repos/{repo}/issues/{int(number)}/comments?per_page=100",
                    "--jq", ".[] | {id, body, user: .user.login, url: .html_url}")
    if not ok:
        return None
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        body = str(c.get("body") or "")
        if str(c.get("user") or "").lower() != login.lower() or marker not in body:
            continue
        m = VERDICT.match(body)
        return {"verdict": m.group(1).strip().replace(" ", "_") if m else "UNREADABLE", "url": c.get("url")}
    return {}


def gate_verdict(gate, repo, number, head):
    """The gate's verdict for the pull request's exact head ({} none yet, None GitHub couldn't be asked)."""
    if gate == "jarbas-local":
        return jarbas_local(repo, number, head)
    return {"verdict": "APPROVED"}


def assign(repo, number, want, have):
    """Adds the profile's assignees the pull request lacks (never takes anyone off). Returns (assignees now, added)."""
    lower = {h.lower() for h in have}
    missing = [x for x in want if x.lower() not in lower]
    if not missing:
        return list(have), []
    ok, _, err = gh("pr", "edit", str(int(number)), "--repo", repo, "--add-assignee", ",".join(missing))
    if not ok:
        raise Fail(f"Couldn't assign pull request #{number} to {', '.join('@' + x for x in missing)}: {err or 'gh failed'}")
    return list(have) + missing, missing


def gate_line(h):
    """The approval in a few words, for the card's text, news and ready's message."""
    gate, verdict, head = h.get("review") or "none", h.get("verdict"), (h.get("head") or "")[:7]
    if gate == "none":
        return "no approval asked"
    name = GATE_NAMES.get(gate, gate)
    if verdict == "APPROVED":
        return f"{name} approved {head}"
    if verdict:
        return f"{name} said {verdict.replace('_', ' ').lower()} at {head}"
    return f"{name} hasn't reviewed {head}"


def handoff_line(t):
    """One line on an Open PR task's hand-off: its pull request, who it's assigned to, its approval, or why blocked."""
    h = t.get("handoff") or {}
    bits = []
    if h.get("pr"):
        bits.append(f"pull request #{h['pr']}" + (f" {h['url']}" if h.get("url") else ""))
    if h.get("assignees"):
        bits.append("assigned to " + ", ".join("@" + x for x in h["assignees"]))
    bits.append(gate_line(h))
    if h.get("blocked"):
        bits.append(f"blocked: {h['blocked']}")
    return "; ".join(bits)


def existing_handoff(s, t, wt, pushed):
    """Verify the already-pushed task's target and exact head before assigning or reading approval."""
    target, branch, start, head = t.get("existing_pr"), t.get("branch"), t.get("start"), t.get("head")
    if not (branch and start and head and head != start
            and git(wt, "symbolic-ref", "--quiet", "HEAD")[1] == f"refs/heads/{branch}"
            and git(wt, "merge-base", "--is-ancestor", start, head)[0]):
        raise Fail("Couldn't verify the task's work on its worktree branch for the existing PR hand-off.")
    remote = remote_of(s.loc["folder"])
    repo = existing_pr_repo(s, list(pushed) if pushed else [remote])
    if target:
        if repo.lower() != target["repo"].lower() or t.get("base") != target["branch"]:
            raise Fail("The task's repository or base branch no longer matches its existing pull request.")
        if not git(wt, "merge-base", "--is-ancestor", target["work"], head)[0]:
            raise Fail("The worktree no longer contains the task's work on its existing pull request.")
    else:
        if pushed.get(remote) != head:
            raise Fail(f"The task's commit {head[:7]} needs to be pushed to its base branch before hand-off.")
        target = existing_pr_target(repo, t["base"])
        target["work"] = head
    facts = pr_facts(target["repo"], target["number"], target)
    if not facts or facts["state"] != "open":
        raise Fail(f"Couldn't verify pull request #{target['number']} is still open on its expected repository and branches.")
    if facts["head"] != head:
        raise Fail(f"Pull request #{target['number']}'s head on GitHub must match the task's commit {head[:7]}.")
    return target, facts


def handoff_gate(s, t, dv, wt, summary, blocked=None, *, pushed=()):
    """Open PR's half of ready, for a worktree task with work of its own: its pull request opened (pushed first) and
    assigned, then its approval read for the exact head it says ready at. Returns why ready is refused (the task stays
    Working, t["handoff"] saying where it stands), or None to hand it off. blocked: the agent's why, handing it off
    though no approval came (its review stopped without one)."""
    from .github import github, github_unreachable, pr_open
    h = dict(t.get("handoff") or {})
    previous_head = h.get("head")
    h.update(review=dv["review"], at=now())
    for k in ("verdict", "comment", "assigned"):
        h.pop(k, None)  # read again for this head: an older head's verdict never stands for it
    if blocked:
        h["blocked"] = " ".join(str(blocked).split())[:300]
    else:
        h.pop("blocked", None)
    t["handoff"] = h
    s.dirty = True
    ref, head = s.ref(t), t.get("head")
    h["head"] = head
    if (pushed or t.get("existing_pr")) and previous_head != head:
        h.pop("tries", None)
    fix = (f" If it can't be sorted out, hand it off as blocked: `herdr-task ready {ref} --blocked \"<why>\"` "
           "(the person gets it as blocked, never merged).")
    if pushed or t.get("existing_pr"):
        try:
            target, facts = existing_handoff(s, t, wt, pushed)
        except Fail as e:
            return str(e)
        t["existing_pr"] = target
        pr, repo = target, target["repo"]
    else:
        repo = github(s)
        if not repo:
            why = github_unreachable(s) or "this repo isn't on GitHub (or gh isn't signed in here)"
            return None if blocked else f"{ref}'s delivery is Open PR, but Herdr can't open its pull request: {why}.{fix}"
        try:
            pr = pr_open(s, t, repo, summary)
        except Fail as e:
            return None if blocked else f"{ref}'s delivery is Open PR, but its pull request didn't open: {e}{fix}"
        facts = pr_facts(repo, pr["number"])
    h.update(pr=pr["number"], url=pr.get("url"), repo=repo)
    if not facts:
        return None if blocked else f"Couldn't read pull request #{pr['number']} on GitHub just now. Run ready again in a moment."
    if facts["state"] == "merged":
        return None  # merged by someone already: ready finds it merged and marks it done (never merged by Herdr)
    if facts["state"] != "open":
        return None if blocked else (f"Pull request #{pr['number']} is closed. Ask the person whether to reopen it; Herdr "
                                     f"never opens a second one for this branch.{fix}")
    if facts["head"] != head:
        return None if blocked else (f"Pull request #{pr['number']}'s head on GitHub is {str(facts['head'])[:7]}, not your "
                                     f"commit {str(head)[:7]}: push your branch (ready does), then run ready again.")
    try:
        h["assignees"], added = assign(repo, pr["number"], dv["assign"], facts["assignees"])
        if added:
            h["assigned"] = added
    except Fail as e:
        h["assignees"] = facts["assignees"]
        return None if blocked else f"{e}. Check the space's Assign PR to setting with the person.{fix}"
    if dv["review"] == "none":
        h["verdict"] = None
        return None
    v = gate_verdict(dv["review"], repo, pr["number"], head)
    if v is None:
        return None if blocked else "Couldn't read its review on GitHub just now. Run ready again in a moment."
    h["verdict"], h["comment"] = v.get("verdict"), v.get("url")
    if h["verdict"] == "APPROVED":
        h.pop("blocked", None)  # approved after all: handed off as approved
        return None
    if blocked:
        return None
    name, url = GATE_NAMES.get(dv["review"], dv["review"]), pr.get("url") or f"#{pr['number']}"
    if h["verdict"]:
        return (f"{name} said {h['verdict'].replace('_', ' ').lower()} for {head[:7]}, the head of pull request {url} "
                f"({h['comment']}), so it isn't approved. Its review loop stopped without approving: don't start it over. "
                f"Say why in your chat, then hand it off as blocked: `herdr-task ready {ref} --blocked \"<why>\"`.")
    h["tries"] = int(h.get("tries") or 0) + 1
    if h["tries"] > MAX_TRIES:
        return (f"Still no {name} verdict for {head[:7]} after {MAX_TRIES} tries. Don't run the review again: hand it off "
                f"as blocked, `herdr-task ready {ref} --blocked \"<why>\"`.")
    from .settings import GATE_WORDS
    return (f"Pull request {url} is open" + (f" and assigned to {', '.join('@' + x for x in h['assignees'])}" if h.get("assignees") else "")
            + f". It waits for {name}'s approval of its exact head {head[:7]}: run "
            f"{GATE_WORDS.get(dv['review'], dv['review']).replace('<its pull request' + chr(39) + 's link>', url)}, then run ready "
            f"again (it reads the verdict on GitHub). Try {h['tries']} of {MAX_TRIES}.")


def accept_check(s, t):
    """Before the person takes an Open PR hand-off (Accept): the pull request read again. Returns (error, merged):
    merged by someone meanwhile is fine (done, merged); closed, or its head moved past what was approved, is refused."""
    h = t.get("handoff") or {}
    target = t.get("existing_pr")
    if target and (target["number"] != h.get("pr") or target["repo"] != h.get("repo")
                   or not t.get("worktree") or git(t["worktree"], "rev-parse", "HEAD")[1] != h.get("head")):
        return "The existing PR hand-off no longer matches the task's worktree. Its agent says ready again first.", False
    if not h.get("pr") or not h.get("repo"):
        return None, False
    facts = pr_facts(h["repo"], h["pr"], target) if target else pr_facts(h["repo"], h["pr"])
    if not facts:
        return f"Couldn't read pull request #{h['pr']} on GitHub just now; try again.", False
    if facts["state"] not in ("open", "merged"):
        return f"Pull request #{h['pr']} was closed since it was handed off.", False
    if (target or facts["state"] == "open") and facts["head"] != h.get("head"):
        return (f"Pull request #{h['pr']} has moved to {str(facts['head'])[:7]} since {str(h.get('head'))[:7]} was handed off"
                + (" and approved" if h.get("verdict") == "APPROVED" else "") + ": its agent says ready again first."), False
    return None, facts["state"] == "merged"


def status(t):
    """The hand-off as the Accept card shows it, read fresh where it can be: {pr, url, assignees, review, verdict, head,
    comment, current (the pull request's head is still the one handed off), blocked, next}."""
    h = dict(t.get("handoff") or {})
    from .settings import delivery_of
    dv = delivery_of(t)
    out = {k: h.get(k) for k in ("pr", "url", "assignees", "review", "verdict", "head", "comment", "blocked")}
    out["review"] = out["review"] or dv["review"]
    out["next"] = dv.get("next") or ""
    out["line"] = gate_line({**h, "review": out["review"]})
    if h.get("pr") and h.get("repo"):
        target = t.get("existing_pr")
        facts = pr_facts(h["repo"], h["pr"], target) if target else pr_facts(h["repo"], h["pr"])
        if facts:
            out["state"] = facts["state"]
            out["current"] = facts["head"] == h.get("head")
            out["assignees"] = facts["assignees"]
    return out

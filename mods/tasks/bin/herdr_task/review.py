"""SHEP-15: Open PR delivery after the hand-off: Awaiting review.

The person's Accept of an Open PR hand-off (delivery.py) whose pull request is open on GitHub no longer marks the task
Done. It moves to its own ongoing state, "awaiting" (Awaiting review), until GitHub says its pull request merged (done,
merged) or the person marks it Done themselves (`herdr-task done`). Closed without merging, it stays there, saying so.
For dependencies (--after) it counts as finished, as the Done it used to be at that Accept.

Its review is a signal within that state, read on GitHub by the tick (review_pass(): one read of one pull request per
due task, bounded backoff when GitHub can't be asked), never written there: Herdr requests, approves and merges
nothing. The record, kept on the task (the public contract the CLI, the Tasks panel and the web app read):

  t["state"]   "awaiting"
  t["awaiting"] = {since, pr, repo, url, head (what was handed off), legacy (moved from an older Done hand-off)}
  t["pr_review"] = {
    state      awaiting_approval | approved | changes_requested | draft | closed | unknown | unavailable
               (merged only on the Done record it leaves behind)
    text       those in words: REVIEW_TEXT
    pr, repo, url, head (the pull request's head when last read), head_moved (not the handed-off head any more)
    decision   GitHub's reviewDecision (APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED) or None: no review rule there
    source     "decision" (GitHub's own) or "reviews" (no rule: the latest review per person, at the head only)
    approvers, requesters   who approved the current head / asks for changes (from "reviews")
    stale_approvals          approvals of an older head, never counted
    checked, ok_at           last try, last good read (ms)
    fails, error, next       failed reads in a row, the last error, when it is read again (ms)
    was                      the last known state while it is unavailable
  }

Approval alone never finishes it: only the merge, verified on GitHub, or the person's Done. An informal review
(Jarbas Local's comment, passing checks, a label) is never GitHub's approval; an approval of an older head, or one
dismissed since, never counts unless GitHub's own review decision says the pull request is approved."""

import json

from .base import now
from .github import gh

AWAITING = "awaiting"
REVIEW_TEXT = {"awaiting_approval": "Awaiting approval", "approved": "Approved", "changes_requested": "Changes requested",
               "draft": "Draft", "closed": "Closed without merging", "merged": "Merged", "unknown": "Checking GitHub",
               "unavailable": "GitHub unavailable"}
REVIEW_EVERY = 2 * 60000       # an open pull request is read again this often
CLOSED_EVERY = 15 * 60000      # a closed one (reopened, it is tracked again)
RETRY_FIRST = 60000            # a failed read: tried again after 1, 2, 4 ... minutes
RETRY_MAX = 30 * 60000         # never more than this apart
UNAVAILABLE_AFTER = 3          # failed reads in a row before the signal says GitHub unavailable (a blip keeps it)
VIEW = "state,isDraft,headRefOid,reviewDecision,mergedAt,mergeCommit,url"
DECISIONS = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes_requested", "REVIEW_REQUIRED": "awaiting_approval"}


def awaiting(t):
    return t.get("state") == AWAITING


def read_pr(repo, number):
    """(the pull request as gh pr view has it, None) or (None, why it couldn't be read)."""
    ok, out, err = gh("pr", "view", str(int(number)), "--repo", repo, "--json", VIEW)
    try:
        v = json.loads(out) if ok else None
    except ValueError:
        v = None
    if not isinstance(v, dict) or not v.get("state"):
        return None, " ".join((err or "gh pr view failed").split())[:200]
    return v, None


def read_reviews(repo, number):
    """(every submitted review, oldest first: [{user, state, commit, at}], None) or (None, why)."""
    ok, out, err = gh("api", "--paginate", f"repos/{repo}/pulls/{int(number)}/reviews?per_page=100",
                      "--jq", ".[] | {user: .user.login, state, commit: .commit_id, at: .submitted_at}")
    if not ok:
        return None, " ".join((err or "gh api failed").split())[:200]
    rows = []
    for line in out.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return sorted(rows, key=lambda r: str(r.get("at") or "")), None


def latest_reviews(rows):
    """Each person's review that stands: their latest approval or change request (a comment after it changes
    nothing, as on GitHub; a dismissal takes it away)."""
    out = {}
    for r in rows or []:
        who, st = str(r.get("user") or ""), str(r.get("state") or "").upper()
        if not who:
            continue
        if st in ("APPROVED", "CHANGES_REQUESTED"):
            out[who] = {"state": st, "commit": r.get("commit")}
        elif st == "DISMISSED":
            out.pop(who, None)
    return out


def signal(v, rows, handed_head):
    """The review signal from what GitHub said: v (gh pr view), rows (its reviews, or None when not asked)."""
    head = v.get("headRefOid")
    decision = str(v.get("reviewDecision") or "").upper() or None
    out = {"head": head, "head_moved": bool(handed_head and head and head != handed_head), "decision": decision,
           "source": None, "approvers": [], "requesters": [], "stale_approvals": 0}
    gh_state = str(v.get("state") or "").upper()
    if gh_state == "MERGED" or v.get("mergedAt"):
        out["state"] = "merged"
    elif gh_state == "CLOSED":
        out["state"] = "closed"
    elif v.get("isDraft"):
        out["state"] = "draft"
    elif decision:
        out["state"], out["source"] = DECISIONS.get(decision, "unknown"), "decision"
    else:
        # No review rule on its base (GitHub gives no decision): the latest review per person, an approval only of the
        # head GitHub has now. Anyone asking for changes outweighs an approval, as GitHub would.
        latest = latest_reviews(rows)
        out["approvers"] = sorted(w for w, r in latest.items() if r["state"] == "APPROVED" and head and r["commit"] == head)
        out["requesters"] = sorted(w for w, r in latest.items() if r["state"] == "CHANGES_REQUESTED")
        out["stale_approvals"] = sum(1 for r in latest.values() if r["state"] == "APPROVED" and r["commit"] != head)
        out["source"] = "reviews"
        out["state"] = "changes_requested" if out["requesters"] else "approved" if out["approvers"] else "awaiting_approval"
    out["text"] = REVIEW_TEXT[out["state"]]
    return out


def read_signal(repo, number, handed_head):
    """(signal, None), or (None, why GitHub couldn't be asked). One gh pr view, and its reviews only when GitHub gives
    no review decision for an open pull request."""
    v, err = read_pr(repo, number)
    if v is None:
        return None, err
    rows = None
    if str(v.get("state") or "").upper() == "OPEN" and not v.get("isDraft") and not v.get("reviewDecision"):
        rows, err = read_reviews(repo, number)
        if rows is None:
            return None, err
    sig = signal(v, rows, handed_head)
    sig["url"] = v.get("url")
    sig["merge_commit"] = (v.get("mergeCommit") or {}).get("oid")
    sig["merged_at"] = v.get("mergedAt")
    return sig, None


def await_review(s, t, since=None, legacy=False):
    """The task moves to Awaiting review for its handed-off pull request (accept_merge(), or an older Done hand-off:
    adopt_legacy()). Its review is read at the next tick."""
    h = t.get("handoff") or {}
    t["state"] = AWAITING
    t["awaiting"] = {"since": since or now(), "pr": h.get("pr"), "repo": h.get("repo"), "url": h.get("url"),
                     "head": h.get("head"), **({"legacy": True} if legacy else {})}
    t["pr_review"] = {"state": "unknown", "text": REVIEW_TEXT["unknown"], "pr": h.get("pr"), "repo": h.get("repo"),
                      "url": h.get("url"), "head": h.get("head"), "head_moved": False, "fails": 0, "next": 0}
    t.pop("done_at", None)
    s.dirty = True


def due(t):
    return awaiting(t) and (t.get("awaiting") or {}).get("pr") and now() >= int((t.get("pr_review") or {}).get("next") or 0)


def review_pass(s, t, report):
    """One read of an awaiting task's pull request, when due: its review signal kept on the task; merged on GitHub:
    done, merged (finish(), as a merge someone else made). GitHub out of reach: tried again later and later
    (RETRY_FIRST doubling up to RETRY_MAX), the signal unavailable once that lasts."""
    if not due(t):
        return
    a = t["awaiting"]
    r = t.setdefault("pr_review", {"state": "unknown", "text": REVIEW_TEXT["unknown"]})
    r.update(checked=now(), pr=a.get("pr"), repo=a.get("repo"))
    s.dirty = True
    sig, err = read_signal(a["repo"], a["pr"], a.get("head"))
    if sig is None:
        r["fails"] = int(r.get("fails") or 0) + 1
        r["error"] = err
        r["next"] = now() + min(RETRY_MAX, RETRY_FIRST * 2 ** (r["fails"] - 1))
        if r["fails"] >= UNAVAILABLE_AFTER and r.get("state") != "unavailable":
            if r.get("state") not in (None, "unknown"):
                r["was"] = r["state"]
            r["state"], r["text"] = "unavailable", REVIEW_TEXT["unavailable"]
            report.setdefault("review_changed", []).append({"ref": s.ref(t), "review": "unavailable"})
        return
    before = r.get("state")
    r.update({k: v for k, v in sig.items() if k not in ("merge_commit", "merged_at")}, fails=0, error=None, ok_at=now(),
             url=sig.get("url") or r.get("url") or a.get("url"))
    r.pop("was", None)
    r["next"] = now() + (CLOSED_EVERY if sig["state"] == "closed" else REVIEW_EVERY)
    if sig["state"] == "merged":
        from .github import finish, watch_main
        t["merged"] = True
        watch_main(s, t, sig.get("merge_commit"))
        from .deploy import applies, await_deploy
        if applies(t):
            # SHEP-16: roboflow/roboflow: merged is not deployed. Awaiting deploy until its deploys pass (deploy.py).
            await_deploy(s, t, sig.get("merge_commit"), sig.get("merged_at"))
            report.setdefault("deploy_changed", []).append({"ref": s.ref(t), "deploy": "unknown"})
            return
        finish(s, t)
        report["done"].append(t["id"])
        return
    if before != sig["state"]:
        report.setdefault("review_changed", []).append({"ref": s.ref(t), "review": sig["state"]})


def mark_done(s, t):
    """The person's Done on an awaiting task (herdr-task done): its worktree goes as the Accept of a hand-off used to
    take it (only when its branch is still at what was handed off and nothing is left uncommitted), else it stays."""
    from .commands import branch_tip
    from .github import finish
    tip, head = branch_tip(s, t), (t.get("awaiting") or {}).get("head") or (t.get("handoff") or {}).get("head")
    if t.get("worktree") and tip and tip == head:
        finish(s, t, even_if_busy=True)


# ---- older hand-offs (SHEP-7's Accept marked them Done) --------------------------------------------------------------
# A task accepted as an Open PR hand-off before this marked Done at once, its pull request still open (ROBO-9). Such a
# genuine hand-off moves to Awaiting review once, if GitHub says its pull request is still open: Done by the Accept
# itself (done_at within a moment of handed_off), never merged, its delivery Open PR, its pull request on record. A task
# made Done by hand, or whose pull request was closed or merged since, is left as it is (asked once: LEGACY_KEY).

LEGACY_WINDOW_MS = 5000
# When each older hand-off is looked at again, kept on its task list (never on the task): {id: ms}, SETTLED once GitHub
# said closed or merged. Its list's own data, as its "github" entry: read by the scheduler and the window's tick alike.
LEGACY_KEY = "awaiting_legacy"
SETTLED = -1


def legacy_candidate(t):
    h = t.get("handoff") or {}
    if t.get("state") != "done" or t.get("merged") or t.get("mode") != "worktree" or t.get("branch_update"):
        return False
    if t.get("pr_review") or t.get("awaiting") or t.get("done_by"):
        return False  # been through Awaiting review already (this code's own record), or Done by the person
    if not (t.get("handed_off") and t.get("accepted_at") and t.get("done_at") and h.get("pr") and h.get("repo")):
        return False
    if abs(int(t["done_at"]) - int(t["handed_off"])) > LEGACY_WINDOW_MS:
        return False
    from .settings import open_pr
    return open_pr(t)


def legacy_due(data, t):
    """data: its task list (a Store's data, or the list read without its lock)."""
    at = ((data or {}).get(LEGACY_KEY) or {}).get(str(t.get("id")), 0)
    return at != SETTLED and now() >= at


def adopt_legacy(s, t, report):
    """One look at an older Done hand-off (legacy_candidate()): its pull request open on GitHub, it moves to Awaiting
    review (and out of the archive); closed or merged, nothing changes on it. Returns whether GitHub was asked."""
    if not legacy_due(s.data, t):
        return False
    seen = s.data.setdefault(LEGACY_KEY, {})
    key = str(t["id"])
    h = t["handoff"]
    v, err = read_pr(h["repo"], h["pr"])
    s.dirty = True
    if v is None:
        n = int(seen.get(key + "#n", 0)) + 1
        seen[key + "#n"] = n
        seen[key] = now() + min(RETRY_MAX, RETRY_FIRST * 2 ** (n - 1))
        return True
    seen.pop(key + "#n", None)
    if str(v.get("state") or "").upper() != "OPEN" or v.get("mergedAt"):
        seen[key] = SETTLED
        return True
    seen.pop(key, None)
    if not seen:
        s.data.pop(LEGACY_KEY, None)
    done_at = t.get("done_at")
    await_review(s, t, since=t.get("handed_off"), legacy=True)
    t["awaiting"]["legacy_done_at"] = done_at
    if t.get("archived"):
        from .archive import unarchive
        unarchive(s, t)
    report.setdefault("awaiting_adopted", []).append(s.ref(t))
    return True


def settled_at(t):
    """When a task stopped needing its agents: Done (done_at) or Awaiting review (awaiting.since); 0 otherwise. Its
    agents end after that, as a Done task's do (tick's end_done())."""
    if t.get("state") == "done":
        return t.get("done_at") or 0
    if t.get("state") == AWAITING:
        return (t.get("awaiting") or {}).get("since") or 0
    if t.get("state") == "deploying":
        return (t.get("deploy") or {}).get("since") or 0
    return 0

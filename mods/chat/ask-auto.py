"""Whether Codex can receive this permission request without a Herdr card."""
import json
import os
from pathlib import Path
import re
import sys

READ_LIMIT = 1024 * 1024
REVIEWABLE = {"Bash", "apply_patch", "write_stdin", "request_permissions"}


def native_review(request):
    # MCP can choose a reviewer per tool. Claude's questions and plans also stay on the card.
    if request.get("hook_event_name") != "PermissionRequest" or request.get("tool_name") not in REVIEWABLE:
        return False
    session, turn = request.get("session_id"), request.get("turn_id")
    if not isinstance(session, str) or not re.fullmatch(r"[0-9a-f-]{36}", session) or not turn:
        return False
    path = Path(request.get("transcript_path") or "")
    if not path.is_absolute() or not path.name.startswith("rollout-") or not path.name.endswith(f"-{session}.jsonl"):
        return False
    if not path.is_file():
        return False
    with path.open("rb") as f:
        header = json.loads(f.readline(READ_LIMIT))
        if header.get("type") != "session_meta" or header.get("payload", {}).get("id") != session:
            return False
        start = f.seek(0, os.SEEK_END) - READ_LIMIT
        f.seek(max(0, start))
        rows = f.read(READ_LIMIT).splitlines()
    # Codex records the effective reviewer per turn, including on resume and after permission changes.
    # Never use a previous turn or infer the reviewer from the hook's "default" permission mode.
    for row in reversed(rows[1:] if start > 0 else rows):
        try:
            entry = json.loads(row)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "turn_context":
            context = entry["payload"]
            if context.get("turn_id") == turn:
                return context.get("approval_policy") == "on-request" and context.get("approvals_reviewer") == "auto_review"
    return False


if __name__ == "__main__":
    try:
        sys.exit(0 if native_review(json.load(sys.stdin)) else 1)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        sys.exit(1)

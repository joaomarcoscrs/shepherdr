"""Pictures the person shared in a chat (SHEP-1): an agent reads them without asking.

A screenshot sent in a chat goes to the agent as its path (mods/chat/attach.js). Claude Code turns a path typed in its
box into an image, but one relayed as text (a task's brief, a message from another agent) is read with its Read tool,
and a file outside the agent's folders asks first: JHQ-6's worker waited on the chat's card for a CleanShot picture the
person had just pasted. Only these are allowed, and only as pictures (an image file by its name and its first bytes):

  - a path the window's chat delivered to an agent on this machine (attach.js deliver writes it to data/chat-shared.tsv:
    "<ms>\\t<path>" lines, kept SHARED_DAYS; a line older is ignored and pruned),
  - a file the chat itself keeps from the person's message box, data/chat-attach/pasted-* (a paste in the window) and
    web-* (an upload from the web app); never data/chat-attach/view/, which links any picture a chat shows,
  - a copy `hc put` landed for an agent on this machine from a chat on another one
    (<state>/herdr-core/<session>/files/<terminal>/<name>).

Nothing else changes: any other file asks as before, and the person's CLI settings are never touched."""

import json
import os
import stat
import sys
import time

from .base import HOME

SHARED_DAYS = 30
# a screenshot is a few MB; anything this big is not one
MAX_BYTES = 64 * 1024 * 1024
EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic")
# the ledger is read from its end: this many bytes at most (thousands of lines)
TAIL = 1 << 20


def app_home():
    return os.environ.get("HERDR_APP_HOME") or os.path.join(HOME, ".config", "herdr-app")


def ledger():
    return os.path.join(app_home(), "data", "chat-shared.tsv")


def put_roots():
    """Where herdr-core keeps what `hc put` sends to this machine's terminals."""
    roots = [os.environ.get("XDG_STATE_HOME") or "", os.path.join(HOME, ".local", "state")]
    return list(dict.fromkeys(os.path.join(r, "herdr-core") for r in roots if r and os.path.isabs(r)))


def picture(path):
    """Whether the file at path is a picture: an image's name and an image's first bytes."""
    if not path.lower().endswith(EXTS):
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff") or head[:6] in (b"GIF87a", b"GIF89a")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or head[:2] == b"BM" or head[:4] in (b"II*\x00", b"MM\x00*")
            or (head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis")))


def listed(real, now=None):
    """Whether the chat delivered this file (its real path) to an agent here in the last SHARED_DAYS."""
    now = time.time() if now is None else now
    try:
        with open(ledger(), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in data.split("\n"):
        at, _, p = line.partition("\t")
        try:
            fresh = now - int(at) / 1000 <= SHARED_DAYS * 86400
        except ValueError:
            continue
        if fresh and p and os.path.isabs(p) and os.path.realpath(p) == real:
            return True
    return False


def kept(real):
    """Whether real is a file the chat keeps from the person's message box, or one `hc put` landed here."""
    head, name = os.path.split(real)
    if head == os.path.realpath(os.path.join(app_home(), "data", "chat-attach")) and name.startswith(("pasted-", "web-")):
        return True
    for root in put_roots():
        rel = os.path.relpath(real, os.path.realpath(root))
        parts = rel.split(os.sep)
        if len(parts) == 4 and parts[1] == "files" and ".." not in parts:
            return True
    return False


def shared_picture(path):
    """Whether path is a picture the person shared in a chat (above): an existing image, at most MAX_BYTES."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        return False
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES or not picture(real):
        return False
    return kept(real) or listed(real)


ALLOW = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}


def allows(req):
    """Whether a PermissionRequest hook's request is only Claude Code's Read of a picture shared in a chat (whole, or
    some of its lines: Read takes no other file)."""
    if not isinstance(req, dict) or req.get("tool_name") != "Read":
        return False
    inp = req.get("tool_input")
    return isinstance(inp, dict) and shared_picture(inp.get("file_path"))


def cmd_shared_read(args):
    """The chat's own hook (mods/chat/ask-hook.sh) asks this first, for any agent: a shared picture never asks."""
    try:
        req = json.loads(sys.stdin.buffer.read(1 << 20) or b"{}")
    except ValueError:
        return
    if allows(req):
        print(json.dumps(ALLOW))

# Cursor Agent

Cursor Agent (`cursor-agent`, or `agent` in newer installs) runs beside Claude Code and Codex, as a task's worker and
as an Assistant. It is offered wherever its CLI is on the machine's PATH (core/agents.js `DEFAULTS` reports `bin`).

## Picking it

- Settings › Agents › Assistant and Workers: the model menu has a Cursor tab (window and web). The Reviewer doesn't
  offer it (core/settings.js `CURSOR_ROLES`): Cursor has no read-only mode in every version of its CLI. The service
  refuses a Cursor reviewer or helper.
- A running Assistant's model menu (its chat) switches it to Cursor, or back. On Cursor, a new model goes in place from
  the web app (`/model`), and as a restart on the same Cursor chat from the window.
- One task: `herdr-task add --agent cursor [--model <id>]`, or `start --agent cursor`.
- A running worker's model menu (its chat) can restart it on Cursor; `herdr-task switch` does the restart.

## An Assistant on Cursor (SHEP-6)

Cursor's own terminal UI keeps no log Shepherdr can read and reports no status, so an Assistant runs
`herdr-task cursor-chat` (mods/tasks/bin/herdr_task/cursor_chat.py). It starts **one** `cursor-agent acp`, Cursor's
Agent Client Protocol server (JSON-RPC over stdio), and is its client:

| What | How |
| --- | --- |
| Messages in | What the chat sends (hc's paste, bracketed: a message of many lines is one) is a `session/prompt`. One sent mid-turn waits its turn (queued). Esc or Ctrl+C stops the turn (`session/cancel`). |
| Conversation out | Cursor's words, tool calls and their results go to a transcript, `<data>/cursor-chats/<chat id>.jsonl` (layout kind `cursor-chats`), held open while it runs: the chat finds it by the process (core/chat-log.js `FIND`) and reads it (`Conversation.cursor`). |
| Status | OSC 7501 with `app=cursor`: working, idle, blocked (`kind=permission`, msg `approve Bash: …`), error; `blocked kind=auth` when Cursor is signed out. |
| Permissions | Cursor asks (`session/request_permission`) for what its allowlist lacks. The bridge shows a menu in its terminal (1 Allow once, 2 Allow always, 3 Reject, Esc rejects) and leaves the request where the chat's cards find Claude Code's hook requests (`data/chat-asks/req/<id>.json`, as mods/chat/ask-hook.sh). A card's Allow is Cursor's *Allow once*; Deny or Dismiss is *Reject*. Whichever answers first wins. It is never answered for the person, not even in Full access. |
| No Auto | Cursor's ACP server ignores Auto-review (below): an Assistant's menus offer Ask and Full access only, Settings refuses Auto for the Assistant, and `--auto-review` given to the bridge runs as Ask and says so in the terminal and the chat. |
| Model | `--model <id>` at start; `/model <id>` in place (the model menu sends it). An id as the menu lists it (`composer-2.5[fast=true]`) or its short name (`composer-2.5`). Cursor saves a session's model as its default in `~/.cursor/cli-config.json`: the bridge puts those keys back, so a chat's model never changes the person's Cursor default. |
| Models listed | Cursor lists the account's models when a session starts; the bridge keeps them in `cursor-chats/models.json`, which the menus read. Before any Cursor chat has run there, the menu lists the CLI's configured model; others are typed by id. |
| Resume | `--resume <chat id>` (`session/load`): a restart, a model menu restart and a revive after a crash continue the same Cursor chat. A chat Cursor can't load starts a new one and says so in the chat and the terminal, with where the old transcript is. |
| Switch from or to Claude/Codex | A restart with a handoff (mods/coordinator/restart.sh): the new agent is told the old one's log, the bridge's transcript included. Conversations don't move between CLIs. |
| Full access | `cursor-agent --force acp` (Cursor's own: its deny list still applies). Ask (the default here) is Cursor's allowlist, asking for the rest. |

## How a worker starts

| Choice | argv |
| --- | --- |
| Model | `--model <id>`; none: Cursor's own (`model.modelId` in `~/.cursor/cli-config.json`) |
| Permissions: Auto (default, SHEP-9) | `--auto-review`: Cursor's Auto-review. Its server classifier runs the tool calls it judges safe; the rest wait for the person in its terminal, with Cursor's reason ("Auto-review: …"). Never `--force` (Cursor refuses the two together). |
| Permissions: Ask | nothing: Cursor's allowlist (`permissions` in cli-config.json), asking in its terminal |
| Permissions: Full access | `--force` (Cursor's deny list still applies) |
| Resume | `--resume <chat id>` |

There is no effort, speed or plan choice in the menus: a "thinking" model or a level is a model id of its own in Cursor
(`claude-opus-5-5[context=300k,effort=medium,fast=false]`).

A task's `--agent cursor` starts on Auto, or on Ask when Settings' worker (another CLI) is set to a mode that asks more
(Ask, Accept edits, Plan, Read only): never more access than chosen. Settings' own Cursor worker keeps the mode saved.
Auto-review needs the account's feature, a model its classifier works with and the team's permission; without them
Cursor itself says so and falls back to its allowlist (Ask), never to more.

## Auto-review and where it works (SHEP-9)

Checked on cursor-agent 2026.11.13, signed in, in scratch folders, with a harmless command its allowlist lacks
(`echo probe > probe-safe.txt`) and a risky one on a path that doesn't exist (`rm -rf ~/<missing>`):

| Path | Auto-review honored? | Seen |
| --- | --- | --- |
| Terminal UI (a task's worker) with `--auto-review` | Yes | `echo` ran with no prompt; `rm -rf ~/…` stopped for approval with "⚠ Auto-review: …". Without the flag, `echo` asked. |
| Terminal UI with `approvalMode: "auto-review"` in cli-config.json | Yes | `echo` ran with no prompt. |
| ACP (`cursor-agent acp`: an Assistant) with `--auto-review` | **No** | `session/request_permission` for the same `echo`, as with no flag. |
| ACP with `approvalMode: "auto-review"` | **No** | the same request. |
| Print mode (`-p`) with `--auto-review` | Yes, but | flagged calls are rejected on the spot: no way to approve, so no approval card. Not used. |

Why: the CLI turns Auto-review on by sending `smartModeClassifierAutoModeEnabled` with each request (its terminal UI's
`getSmartModeClassifierAutoModeEnabled`); its ACP sessions (src/acp in the bundle) never set it, and read only `force`
from the command line. ACP's session config options are only `mode` (agent, plan, ask) and `model`. If a later Cursor
honors it over ACP, `core/agents.js` `CURSOR_CHAT_PERMS` and the bridge's `AUTO_IGNORED` are the two places to change.

## Sessions

herdr-task reads a live Cursor worker's chat id from the store it holds open (`~/.cursor/chats/<hash>/<id>/store.db`),
an Assistant's from its bridge transcript, and resumes it with `--resume`. With no chat id known, a restart starts fresh
with a handoff.

## Limits

- Cursor's CLI must be signed in (`cursor-agent login`, in a terminal, by the person). Signed out, the bridge stops,
  says so, and reports `blocked kind=auth`.
- A worker (Cursor's own UI) reports no status to Herdr: it reads Working while its agent runs, Needs you (Agent
  stopped) once it exits, and Ready once it says so; its conversation isn't shown in the chat (the Terminal view
  works). The brief tells it so (launch.py `cursor_line`). An Assistant has none of these limits.
- No rewind, no fork, no slash-command list, no usage limits, no Backup takeover to or from Cursor.
- No reasoning level or Fast row in the menus; Cursor's levels are part of its model ids.
- Pictures pasted into the chat reach Cursor as their paths (it reads them with its own tools), not as images.
- Whether `--force` changes what Cursor's ACP server asks was not checked against a real session (its permission
  requests still go to the person).
- No Auto for an Assistant on Cursor (its ACP server ignores Auto-review, above).
- A worker on Auto shows "Auto" in the menus only when its argv has `--auto-review` or Cursor's own approvalMode says
  auto-review; Cursor's quiet fallback to its allowlist (account, model or team without Auto-review) isn't seen.
- Cursor's own `attribution` settings add a co-author trailer to its commits unless turned off in cli-config.json.

## For another model menu (joão HQ's jhq-models)

The chat gives its model menu (mods/chat-models, or a clone) these props; nothing else changed:

- `menus.cursor` is now there for an Assistant and any chat agent but a reviewer or helper, where Cursor's CLI is
  installed. Its `models` are the account's (from `cursor-chats/models.json`) once a Cursor chat has run on that
  machine, ids as Cursor gives them (`composer-2.5[fast=true]`), `name` its short name; `efforts: []`, `fast: false`;
  `perms`: `manual` (Ask), `auto` (Auto: `--auto-review`, SHEP-9) and `full` (Full access) for a task's worker, without
  `auto` for an Assistant's chat (core/agents.js `menuFor(..., { chat })`). `permDefault.cursor` is `auto`.
- `permAcross(perm, agent, permDefault, perms)` takes the target menu's `perms` as a fourth argument (SHEP-9) and fits
  the mode to them (`fitPerm`: the mode, else the default, else the first, Ask). Claude's or Codex's `auto` now carries
  to Cursor's `auto`; Accept edits, Plan and Read only to Ask.
- `current.chat: true` when the running agent is Cursor behind the bridge: it can take a model change in place, by
  `/model <id>` typed into it (the web's model menu does so: core/live-switch.js `liveOK`). Shepherdr's window chat
  restarts it on the same Cursor chat instead (`--resume`). Another permission mode, or another CLI, always restarts.
  Without `current.chat` (a worker), any change restarts.
- A pick of Cursor for an Assistant starts `herdr-task cursor-chat …` (core/agents.js `argvFor(pick, old, { chat:
  true })`): its conversation shows in the chat, so a "Cursor shows in Terminal" note is only true for workers.

Follow-up for jhq-models (read in SHEP-9, not changed: it lives in ~/.config/herdr-app, outside this repo). Its menus and
perms come from the chat's props, so a task's Cursor shows Auto there with no change, and an Assistant's doesn't. Its
frozen `core/agents.js` copy still has the old `PERM_ACROSS.cursor` (`auto: "manual"`) and a three-argument
`permAcross`, so switching to Cursor from Claude's or Codex's Auto lands on Ask, never on Auto. That is safe, but Auto
isn't carried over. To match: take `PERM_ACROSS`, `CURSOR_CHAT_PERMS`, `fitPerm` and `permAcross` from this file, and
call `permAcross(was.perm, m.agent, this.p.permDefault, this.p.menus?.[m.agent]?.perms)` in `choose()` (main.js). Also
`kinds.js` says "Ask or Full access" for Cursor; it should say "Ask, Auto (a task's worker) or Full access".

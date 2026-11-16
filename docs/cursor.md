# Cursor Agent

Cursor Agent (`cursor-agent`, or `agent` in newer installs) runs a task's worker beside Claude Code and Codex. It is
offered wherever its CLI is on the machine's PATH (core/agents.js `DEFAULTS` reports `bin`).

## Picking it

- Settings › Agents › Workers: the model menu has a Cursor tab (window and web). Assistant and Reviewer don't offer it
  (core/settings.js `forRole`), and the service refuses a Cursor Assistant or Reviewer.
- One task: `herdr-task add --agent cursor [--model <id>]`, or `start --agent cursor`.
- A running worker's model menu (its chat) can restart it on Cursor; `herdr-task switch` does the restart.

## How it starts

| Choice | argv |
| --- | --- |
| Model | `--model <id>`; none: Cursor's own (`model.modelId` in `~/.cursor/cli-config.json`) |
| Permissions: Ask (default) | nothing: Cursor's allowlist (`permissions` in cli-config.json), asking in its terminal |
| Permissions: Full access | `--force` (Cursor's deny list still applies) |
| Resume | `--resume <chat id>` |

There is no effort, speed or plan choice: Cursor's CLI has none in every version (a "thinking" model is a model id of
its own). Its menu lists the configured model; any other is typed by id ("Use the id typed").

## Sessions

herdr-task reads a live Cursor agent's chat id from the store it holds open (`~/.cursor/chats/<hash>/<id>/store.db`)
and resumes it with `--resume`. A transcript (`~/.cursor/projects/*/agent-transcripts/<id>.jsonl`) is recorded as its log
where the CLI writes one. With no chat id known, a restart starts fresh with a handoff.

## Limits

- Cursor reports no status to Herdr (no OSC 7501): a Cursor task reads Working while its agent runs, Needs you
  (Agent stopped) once it exits, and Ready once it says so. A turn that ends without `ready`, or a permission it
  asks for in its terminal, is not shown as Needs you. The brief tells it so (launch.py `cursor_line`).
- Its conversation isn't shown in the chat: the chat says it can't read this agent, and the Terminal view works.
- In Ask mode every command its allowlist lacks, `herdr-task status` and `ready` too, waits for a yes in its terminal.
- No in-place model switch (a restart resumes the chat), no fork, no slash-command list, no usage limits, no Backup
  takeover to or from Cursor.
- Cursor's own `attribution` settings add a co-author trailer to its commits unless turned off in cli-config.json.

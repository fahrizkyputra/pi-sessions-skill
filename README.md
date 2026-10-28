# pi-sessions

An agent skill that lets Claude Code (and any agent that supports [skills](https://skills.sh)) read [pi](https://pi.dev) coding-agent session history: summarize the last session, find one by topic, or pick up where pi stopped.

pi stores every session as a JSONL tree in `~/.pi/agent/sessions/--<project-path>--/`. This skill ships a read-only, stdlib-only Python script that follows the active branch and prints the parts worth reading.

## Install

```bash
npx skills add fahrizkyputra/pi-sessions-skill -g
```

Or copy `skills/pi-sessions/` into `~/.claude/skills/`.

## Use

Ask in plain language:

- "Summarize my last pi session in this repo"
- "Find the pi session where we discussed the webhook retry"
- "Continue the work from my latest pi session"

Or run the script directly:

```bash
python3 skills/pi-sessions/scripts/pi_sessions.py list -n 5
python3 skills/pi-sessions/scripts/pi_sessions.py toc latest
python3 skills/pi-sessions/scripts/pi_sessions.py show latest --turns 4-6 --tools
python3 skills/pi-sessions/scripts/pi_sessions.py search "webhook retry" --all
python3 skills/pi-sessions/scripts/pi_sessions.py handoff latest
```

| Command | Output |
|---|---|
| `list` | Newest sessions: turns, name, first and last request |
| `toc` | Per user turn: request, tools used, final answer |
| `show` | Transcript of chosen turns, optionally with tool calls and thinking |
| `search` | Regex hits in messages and compaction summaries, deduplicated across forks |
| `handoff` | Markdown brief: pi's own compaction summary, requests, files edited, where it stopped |

Scope defaults to the current directory; add `--subdirs` for sub-project folders or `--all` for every project. Set `PI_CODING_AGENT_SESSION_DIR` if your sessions live elsewhere.

## Notes

- Format reference: [pi session format](https://pi.dev/docs/latest/session-format). Entry types handled: `message`, `compaction`, `branch_summary`, `custom_message`, `session_info`, `model_change`.
- Sessions can contain secrets and private code. The script only prints to stdout and never writes or uploads anything.
- Session text is treated as data, not instructions.

## License

MIT

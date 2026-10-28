#!/usr/bin/env python3
"""Read pi (pi.dev) coding-agent sessions: list, toc, show, search, handoff.

Sessions are JSONL trees under ~/.pi/agent/sessions/--<cwd>--/. Every command
reads the active branch (leaf -> root via parentId), the same path pi replays.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

SESSION_ROOT = Path(
    os.environ.get("PI_CODING_AGENT_SESSION_DIR") or Path.home() / ".pi/agent/sessions"
)
CONTEXT_TYPES = {"message", "compaction", "branch_summary", "custom_message"}
PATH_KEYS = ("path", "file_path", "filePath")
EDIT_TOOLS = {"edit", "write"}


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.header: dict = {}
        self.entries: list[dict] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session":
                    self.header = obj
                else:
                    self.entries.append(obj)

    @property
    def name(self) -> str:
        names = [e["name"] for e in self.entries if e.get("type") == "session_info" and e.get("name")]
        return names[-1] if names else ""

    def branch(self) -> list[dict]:
        by_id = {e["id"]: e for e in self.entries if "id" in e}
        leaf = next((e for e in reversed(self.entries) if e.get("type") in CONTEXT_TYPES), None)
        path, seen = [], set()
        while leaf is not None and leaf["id"] not in seen:
            seen.add(leaf["id"])
            path.append(leaf)
            leaf = by_id.get(leaf.get("parentId"))
        path.reverse()
        return path


def blocks(msg: dict) -> list[dict]:
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content or []


def text_of(msg: dict, thinking: bool = False) -> str:
    parts = []
    for b in blocks(msg):
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "thinking" and thinking:
            parts.append(f"[thinking] {b.get('thinking', '')}")
    text = "\n".join(p for p in parts if p).strip()
    if not text and any(b.get("type") == "image" for b in blocks(msg)):
        return "[image]"
    return text


def tool_calls(msg: dict) -> list[dict]:
    return [b for b in blocks(msg) if b.get("type") == "toolCall"]


def squash(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_brief(call: dict, limit: int) -> str:
    args = call.get("arguments") or {}
    if call.get("name") == "bash":
        detail = args.get("command", "")
    else:
        detail = next((args[k] for k in PATH_KEYS if k in args), json.dumps(args, ensure_ascii=False))
    return f"{call.get('name')}: {squash(str(detail), limit)}"


def turns(branch: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in branch:
        if e["type"] != "message":
            continue
        msg = e["message"]
        role = msg.get("role")
        if role == "user":
            out.append({"n": len(out) + 1, "ts": e["timestamp"], "user": text_of(msg), "msgs": []})
        elif out and role in ("assistant", "toolResult"):
            out[-1]["msgs"].append(msg)
    return out


def turn_tools(turn: dict) -> Counter:
    return Counter(c["name"] for m in turn["msgs"] if m.get("role") == "assistant" for c in tool_calls(m))


def turn_answer(turn: dict) -> str:
    for m in reversed(turn["msgs"]):
        if m.get("role") == "assistant" and text_of(m):
            return text_of(m)
    return ""


def project_slug(cwd: str) -> str:
    return "--" + re.sub(r"[/\\:]", "-", cwd.lstrip("/\\")) + "--"


def session_files(cwd: str, scope: str) -> list[Path]:
    if not SESSION_ROOT.is_dir():
        return []
    slug = project_slug(cwd)
    dirs = []
    for d in SESSION_ROOT.iterdir():
        if not d.is_dir():
            continue
        if scope == "all" or d.name == slug or (scope == "subdirs" and d.name.startswith(slug[:-2])):
            dirs.append(d)
    files = [f for d in dirs for f in d.glob("*.jsonl")]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def resolve(ref: str, args) -> Path:
    for candidate in (Path(ref).expanduser(), Path(ref + ".jsonl").expanduser()):
        if candidate.is_file():
            return candidate
    if ref == "latest":
        files = session_files(args.cwd, "cwd")
    else:  # a session id (full or unique prefix) is global: no --cwd needed
        every = session_files(args.cwd, "all")
        files = [f for f in every if f.stem.partition("_")[2] == ref] or [f for f in every if ref in f.name]
    if not files:
        sys.exit(f"no pi session matches {ref!r} (looked under {SESSION_ROOT})")
    if len(files) > 1 and ref != "latest":
        listing = "\n".join(f"  {f.stem.partition('_')[2]}  {f.parent.name}" for f in files[:8])
        sys.exit(f"{ref!r} matches {len(files)} sessions; use a longer id:\n{listing}")
    return files[0]


def stamp(iso: str) -> str:
    return iso[:16].replace("T", " ")


def cmd_list(args) -> None:
    files = session_files(args.cwd, args.scope)[: args.limit]
    if not files:
        sys.exit(f"no pi sessions for {args.cwd} under {SESSION_ROOT} (try --subdirs or --all)")
    for f in files:
        s = Session(f)
        ts = turns(s.branch())
        print(f"{f}\n  {stamp(s.header.get('timestamp', ''))} · {len(ts)} turns · {s.name or '(unnamed)'}")
        print(f"  cwd: {s.header.get('cwd', '?')}")
        if ts:  # forks inherit their parent's history, so the last request tells sessions apart
            print(f"  first: {squash(ts[0]['user'], 90)}\n  last:  {squash(ts[-1]['user'], 90)}")


def cmd_toc(args) -> None:
    s = Session(resolve(args.session, args))
    print(f"# {s.path.name} · {s.name or '(unnamed)'} · cwd {s.header.get('cwd', '?')}")
    for t in turns(s.branch()):
        tools = ", ".join(f"{k}×{v}" for k, v in turn_tools(t).most_common(4))
        print(f"\n[{t['n']}] {stamp(t['ts'])}  {squash(t['user'], args.width)}")
        if tools:
            print(f"    tools: {tools}")
        answer = turn_answer(t)
        if answer:
            print(f"    → {squash(answer, args.width)}")


def parse_range(spec: str | None, total: int) -> range:
    if not spec:
        return range(1, total + 1)
    lo, _, hi = spec.partition("-")
    return range(int(lo or 1), int(hi or lo or total) + 1)


def cmd_show(args) -> None:
    s = Session(resolve(args.session, args))
    selected = parse_range(args.turns, len(turns(s.branch())))
    for t in turns(s.branch()):
        if t["n"] not in selected:
            continue
        print(f"\n## [{t['n']}] {stamp(t['ts'])} USER\n{squash_block(t['user'], args.max_chars)}")
        for m in t["msgs"]:
            if m["role"] == "assistant":
                text = text_of(m, args.thinking)
                if text:
                    print(f"\nASSISTANT\n{squash_block(text, args.max_chars)}")
                if args.tools:
                    for c in tool_calls(m):
                        print(f"  → {tool_brief(c, 200)}")
            elif m["role"] == "toolResult" and args.tools:
                flag = " ERROR" if m.get("isError") else ""
                print(f"  ← {m.get('toolName')}{flag}: {squash(text_of(m), 160)}")


def squash_block(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [{len(text) - limit} more chars]"


def cmd_search(args) -> None:
    pattern = re.compile(args.query, re.IGNORECASE)
    shown = 0
    seen: set[str] = set()  # forks inherit parent turns; report each hit once, newest session first
    for f in session_files(args.cwd, args.scope):
        if not pattern.search(f.read_text(encoding="utf-8", errors="ignore")):
            continue
        s = Session(f)
        hits = []
        turn_no = 0
        for e in s.branch():
            if e["type"] == "message" and e["message"].get("role") == "user":
                turn_no += 1
            text = (
                e["summary"] if e["type"] in ("compaction", "branch_summary")
                else text_of(e["message"]) if e["type"] == "message" and e["message"].get("role") in ("user", "assistant")
                else ""
            )
            m = pattern.search(text)
            if m:
                role = e["message"]["role"] if e["type"] == "message" else e["type"]
                lo = max(0, m.start() - 60)
                snippet = squash(text[lo:m.end() + 100], 170)
                if snippet not in seen:
                    seen.add(snippet)
                    hits.append(f"  [turn {turn_no}] {role}: …{snippet}")
        if not hits:
            continue
        print(f"{f}\n  {stamp(s.header.get('timestamp', ''))} · {s.name or '(unnamed)'}")
        print("\n".join(hits[: args.hits]))
        shown += 1
        if shown >= args.limit:
            break
    if not shown:
        print(f"no matches for {args.query!r} (scope: {args.scope}; widen with --subdirs or --all)")


def cmd_handoff(args) -> None:
    s = Session(resolve(args.session, args))
    branch = s.branch()
    ts = turns(branch)
    models = list(dict.fromkeys(e["modelId"] for e in s.entries if e.get("type") == "model_change"))
    edited = Counter(
        next((c["arguments"][k] for k in PATH_KEYS if k in (c.get("arguments") or {})), None)
        for e in branch if e["type"] == "message" and e["message"].get("role") == "assistant"
        for c in tool_calls(e["message"]) if c.get("name") in EDIT_TOOLS
    )
    edited.pop(None, None)
    compactions = [e for e in branch if e["type"] == "compaction"]

    print(f"# Pi session handoff — {s.name or s.path.name}")
    print(f"- file: {s.path}\n- cwd: {s.header.get('cwd', '?')}")
    print(f"- span: {stamp(branch[0]['timestamp'])} → {stamp(branch[-1]['timestamp'])} · {len(ts)} turns")
    if models:
        print(f"- models: {', '.join(models)}")
    if compactions:
        print(f"\n## Pi's own summary of earlier work (latest of {len(compactions)} compactions)")
        print(squash_block(compactions[-1]["summary"].strip(), args.summary_chars))
    print("\n## User requests, in order")
    shown = ts if len(ts) <= args.requests else [ts[0]] + ts[-(args.requests - 1):]
    for t in shown:
        print(f"[{t['n']}] {squash(t['user'], 400)}")
    if len(shown) < len(ts):
        print(f"(first request plus last {args.requests - 1} of {len(ts)}; `show --turns A-B` for the rest)")
    if edited:
        print("\n## Files edited or written")
        print("\n".join(f"- {p} ×{n}" for p, n in edited.most_common(args.files)))
    if ts:
        last = ts[-1]
        print(f"\n## Where it stopped — turn {last['n']}\nUSER: {squash_block(last['user'], 1500)}")
        answer = turn_answer(last)
        print(f"ASSISTANT: {squash_block(answer, 3000)}" if answer else "ASSISTANT: (no text reply)")
        final = next((m for m in reversed(last["msgs"]) if m.get("role") == "assistant"), None)
        if final and final.get("stopReason") == "error":
            print(f"ENDED ON ERROR: {squash(final.get('errorMessage', ''), 300)}")


def cmd_to_pi(args) -> None:
    src = resolve(args.session, args)
    s = Session(src)
    brief = sys.stdin.read() if args.brief == "-" else Path(args.brief).expanduser().read_text(encoding="utf-8")
    if not brief.strip():
        sys.exit("empty brief: write what changed since the session ended, then retry")
    sid = s.header.get("id", src.stem.partition("_")[2])
    body = (
        f"# Handoff from Claude Code\n\nSession `{sid}` ({s.name or 'unnamed'}) continued in Claude Code after you stopped. "
        "Its report follows. Treat it as a status report: check the repo (`git status`, the files named) before acting.\n\n"
        f"{brief.strip()}\n"
    )
    out = Path(tempfile.gettempdir()) / f"pi-handoff-{sid[:8]}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    cwd = s.header.get("cwd") or args.cwd
    print(f"brief written: {out}\noriginal session untouched: {src}\n")
    print("Run in a separate terminal (pi forks the original into a new session):")
    print(f"  cd {shlex.quote(cwd)} && pi --fork {shlex.quote(str(src))} @{shlex.quote(str(out))} {shlex.quote(args.message)}")


def main() -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # quiet `| head`
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=os.getcwd(), help="project whose sessions to read (default: current dir)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def scoped(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--subdirs", dest="scope", action="store_const", const="subdirs", help="include sub-project folders")
        g.add_argument("--all", dest="scope", action="store_const", const="all", help="every project")
        p.set_defaults(scope="cwd")

    p = sub.add_parser("list", help="newest sessions first")
    scoped(p)
    p.add_argument("-n", "--limit", type=int, default=10)
    p.set_defaults(fn=cmd_list)

    session_help = "session id (full or unique prefix), 'latest', or a file path"
    p = sub.add_parser("toc", help="one block per user turn")
    p.add_argument("session", help=session_help)
    p.add_argument("--width", type=int, default=160)
    p.set_defaults(fn=cmd_toc)

    p = sub.add_parser("show", help="readable transcript")
    p.add_argument("session", help=session_help)
    p.add_argument("--turns", help="e.g. 5, 5-9, 12-")
    p.add_argument("--tools", action="store_true", help="include tool calls and results")
    p.add_argument("--thinking", action="store_true")
    p.add_argument("--max-chars", type=int, default=2500, help="per message")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("search", help="regex over user/assistant text and summaries")
    p.add_argument("query")
    scoped(p)
    p.add_argument("-n", "--limit", type=int, default=8, help="max sessions")
    p.add_argument("--hits", type=int, default=3, help="max hits per session")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("handoff", help="brief for continuing the work elsewhere")
    p.add_argument("session", help=session_help)
    p.add_argument("--requests", type=int, default=12)
    p.add_argument("--files", type=int, default=25)
    p.add_argument("--summary-chars", type=int, default=6000)
    p.set_defaults(fn=cmd_handoff)

    p = sub.add_parser("to-pi", help="prepare a fork command that resumes the session in pi with Claude Code's report")
    p.add_argument("session", help=session_help)
    p.add_argument("--brief", default="-", help="markdown file with what changed since, or '-' for stdin")
    p.add_argument("--message", default="Lanjutkan dari handoff Claude Code di atas. Verifikasi kondisi repo dulu.")
    p.set_defaults(fn=cmd_to_pi)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

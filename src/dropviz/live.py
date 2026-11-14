"""The live "working now" layer: which tool and model works on which feature, where, since when.

Read-only and local. Only timestamps, cwd, branch and model names are read from AI-tool records; message text is
never kept. Sources: Claude Code transcripts, Codex rollouts and the OpenCode database whose files changed in the
last `minutes`, plus the live MCP claims of this repo. Remote hosts (config `remote_hosts:`) are off by default; when
listed, dropviz only asks them (read-only ssh `find`) whether a transcript changed recently."""
import json
import os
import socket
import sqlite3
import subprocess
import time

from . import core, defaults, tokens

STALL = defaults.get("live_stall_minutes") * 60
TAIL = defaults.get("live_tail_kb") * 1024


def _tail_lines(path, size=TAIL):
    try:
        with open(path, "rb") as f:
            n = os.fstat(f.fileno()).st_size
            f.seek(max(0, n - size))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if n > size:
        lines = lines[1:]  # the first line may be cut
    return lines


def _head_ts(path):
    try:
        with open(path, "rb") as f:
            for line in f.readlines(65536)[:50]:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ts = tokens.parse_ts(d.get("timestamp") or (d.get("payload") or {}).get("timestamp")) if isinstance(d, dict) else None
                if ts:
                    return ts
    except OSError:
        pass
    return None


def _claude(base, since):
    out = []
    for path in tokens._files(base, ".jsonl", since):
        last, model = None, ""
        for line in _tail_lines(path):
            if b'"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") if isinstance(d, dict) else None
            if d.get("type") != "assistant" or not isinstance(msg, dict):
                continue
            ts = tokens.parse_ts(d.get("timestamp"))
            if ts:
                model = tokens._model(msg.get("model")) or model
                last = (ts, d.get("cwd"), d.get("gitBranch"))
        if last:
            out.append({"tool": "claude", "last": last[0], "cwd": last[1], "branch": last[2], "model": model or tokens.UNKNOWN_MODEL,
                        "since": _head_ts(path) or last[0], "session": path})
    return out


def _codex(base, since):
    out = []
    for path in tokens._files(base, ".jsonl", since):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                first = json.loads(f.readline())
        except (OSError, ValueError):
            continue
        p = first.get("payload") if isinstance(first, dict) else None
        if not isinstance(p, dict) or first.get("type") != "session_meta":
            continue
        model, last = "", None
        for line in _tail_lines(path):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            ts = tokens.parse_ts(d.get("timestamp"))
            if ts:
                last = ts
            pl = d.get("payload")
            if d.get("type") == "turn_context" and isinstance(pl, dict):
                model = tokens._model(pl.get("model")) or model
        if last:
            out.append({"tool": "codex", "last": last, "cwd": p.get("cwd"), "branch": None, "model": model or tokens.UNKNOWN_MODEL,
                        "since": tokens.parse_ts(p.get("timestamp")) or last, "session": path})
    return out


def _opencode(db, since):
    out = []
    if not os.path.isfile(db):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3)
        rows = con.execute("select session_id, time_created, data from message where time_created >= ? order by time_created",
                           (int(since * 1000),)).fetchall()
        con.close()
    except sqlite3.Error:
        return out
    by = {}
    for sid, tc, data in rows:
        try:
            d = json.loads(data)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get("role") != "assistant":
            continue
        mdl = tokens._model("/".join(x for x in (d.get("providerID"), d.get("modelID")) if isinstance(x, str) and x))
        e = by.setdefault(sid, {"tool": "opencode", "since": tokens.parse_ts(tc), "model": ""})
        e.update(last=tokens.parse_ts(tc), cwd=(d.get("path") or {}).get("cwd"), branch=None, session="opencode:" + str(sid))
        e["model"] = mdl or e["model"]
    for e in by.values():
        e["model"] = e["model"] or tokens.UNKNOWN_MODEL
        out.append(e)
    return out


def _branch_features(root, default, branch, feats):
    """Feature ids whose paths the branch changed (vs the default branch)."""
    files = core.git(root, "diff", "--name-only", "--no-renames", "%s...%s" % (default, branch), check=False).split("\n")
    files = [x for x in files if x]
    ids = []
    for f in feats:
        hit = core._matcher(f.get("paths") or [])
        if f.get("paths") and any(hit(x) for x in files):
            ids.append(f["id"])
    return ids


REMOTE_DIRS = [defaults.data()["sources"][k] for k in ("claude_dir", "codex_dir")]  # the same default folders, on the other host


def _remote(hosts, minutes):
    out = []
    for h in hosts:
        h = str(h)
        if not h or h.startswith("-") or any(c.isspace() for c in h):
            continue
        try:
            p = subprocess.run(["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", h,
                                "find %s -name '*.jsonl' -mmin -%d 2>/dev/null | head -20" % (" ".join(REMOTE_DIRS), minutes)],
                               capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            continue
        if p.returncode == 0 and p.stdout.strip():
            out.append({"tool": "remote", "model": tokens.UNKNOWN_MODEL, "machine": h, "features": [], "branch": None,
                        "worktree": None, "since": None, "last": None, "count": len(p.stdout.split()), "window": minutes})
    return out


def working_now(root, state, minutes=None, now=None, sources=None):
    minutes = minutes or defaults.get("live_window_minutes")
    """-> {"entries": [...], "by_feature": {feature id: {model, tool, machine, stalled, last}}, "stall_minutes": 30}"""
    now = time.time() if now is None else now
    src = dict(tokens.default_sources(), **(sources or {}))
    since = now - minutes * 60
    repo = tokens.Repo(root)
    recs = _claude(src["claude"], since) + _codex(src["codex"], since) + _opencode(src["opencode"], since)
    host = defaults.get("machine_name") or socket.gethostname().split(".")[0]
    entries = []
    for r in recs:
        mem = repo.member(r["cwd"])
        if not mem or r["last"] < since:
            continue
        wt, cur = mem
        br = r["branch"] if r["branch"] and r["branch"] != "HEAD" else cur
        ids = []
        if br and br != state["default_branch"] and br in repo.branches:
            ids = _branch_features(root, state["default_branch"], br, state["features"])
        entries.append({"tool": r["tool"], "model": r["model"], "machine": host, "kind": "session", "features": ids,
                        "branch": br, "worktree": wt, "since": r["since"], "last": r["last"],
                        "stalled": now - r["last"] > STALL, "minutes": int((now - r["since"]) / 60) if r["since"] else None})
    for fid, c in core.live_claims(root, now).items():
        entries.append({"tool": "mcp claim", "model": c.get("session", ""), "machine": host, "kind": "claim", "features": [fid],
                        "branch": None, "worktree": None, "since": c.get("since"), "last": c.get("last"),
                        "stalled": now - c.get("last", now) > STALL, "minutes": int((now - c.get("since", now)) / 60),
                        "note": c.get("note", "")})
    hosts = core.load_config(root).get("remote_hosts") or []
    entries += _remote([hosts] if isinstance(hosts, str) else hosts, minutes) if hosts else []
    entries.sort(key=lambda e: -(e["last"] or 0))
    by = {}
    for e in entries:
        for fid in e["features"]:
            cur = by.get(fid)
            if cur is None or (cur["stalled"] and not e["stalled"]):
                by[fid] = {"model": e["model"], "tool": e["tool"], "machine": e["machine"], "stalled": e["stalled"], "last": e["last"]}
    return {"entries": entries, "by_feature": by, "stall_minutes": STALL // 60, "window_minutes": minutes}

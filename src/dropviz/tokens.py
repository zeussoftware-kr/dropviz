"""Tokens per commit (PLAN A). Read-only readers for Claude Code, Codex and OpenCode records.

Only token counts, timestamps, cwd and branch names are kept; message text is never stored.
Every record that belongs to the repo ends up in exactly one place: a commit, or the explicit
"unattributed" bucket (with a reason). Nothing is dropped, nothing is counted twice."""
import bisect
import datetime
import fnmatch
import json
import os
import re
import sqlite3
import time

from . import core, defaults, i18n

NOTES_REF = "dropviz-tokens"
TOOL_NAMES = ("claude", "codex", "opencode")
FIELDS = ("input", "cache_read", "cache_write", "output")
COMMIT_CALL = re.compile(r"\bgit(?:\s+-[cC]\s+\S+|\s+--?\S+)*\s+commit\b")
WINDOW = defaults.get("commit_call_window_seconds")  # a `git commit` tool call counts as the boundary of a commit made up to this many seconds later
EPS = 0.999   # commit times have 1 s resolution


def default_sources():
    return {"claude": defaults.source("claude_dir"), "codex": defaults.source("codex_dir"),
            "opencode": defaults.source("opencode_db")}


def parse_ts(s):
    if isinstance(s, (int, float)):
        return float(s) / 1000.0 if s > 1e11 else float(s)
    try:
        d = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def parse_since(text):
    if not text:
        return None
    try:
        d = datetime.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ValueError(i18n.t(i18n.from_env(), "err.since", text=text))
    return d.timestamp()  # local midnight


def _num(x):
    return x if isinstance(x, int) and x > 0 else 0


# ---------- which records belong to this repo ----------
GENERIC = {"main", "master", "HEAD", "trunk", "develop", "dev"}


class Repo:
    def __init__(self, root, worktree_roots=None):
        self.root = os.path.realpath(root)
        self.globs = [os.path.expanduser(g) for g in (worktree_roots or [])]
        self.worktrees = []  # (realpath, branch or None)
        path, branch = None, None
        text = core.git(root, "worktree", "list", "--porcelain", check=False) + "\n"
        for line in text.splitlines():
            if line.startswith("worktree "):
                path, branch = os.path.realpath(line[9:]), None
            elif line.startswith("branch refs/heads/"):
                branch = line[len("branch refs/heads/"):]
            elif line == "" and path:
                self.worktrees.append((path, branch))
                path = None
        if not any(p == self.root for p, _ in self.worktrees):
            self.worktrees.append((self.root, None))
        self.worktrees.sort(key=lambda w: -len(w[0]))
        self.branches = set(core.git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads", check=False).split())
        self.default = core.default_branch(root)
        self._member = {}
        self._reflog = {}
        self._chain = {}
        self._known = None
        self._merges = None

    def known_refs(self):
        """Branch names that exist or existed here: local, remote, `Merge branch 'x'` subjects, HEAD reflog."""
        if self._known is None:
            k = set(self.branches)
            for r in core.git(self.root, "for-each-ref", "--format=%(refname:short)", "refs/remotes", check=False).split():
                if "/" in r and not r.endswith("/HEAD"):
                    k.add(r.split("/", 1)[1])
            for rev, subj in self.merges().items():
                k.add(rev)
            for line in core.git(self.root, "reflog", "show", "--format=%gs", "HEAD", check=False).splitlines():
                m = re.match(r"checkout: moving from (.+) to (.+)$", line)
                if m:
                    k.update(m.groups())
            self._known = k
        return self._known

    def strong(self):
        """Names that count as evidence for a record whose directory is gone (never the default/generic ones)."""
        return {b for b in self.known_refs() if b not in GENERIC and b != self.default and not re.fullmatch(r"[0-9a-f]{7,40}", b)}

    def merges(self):
        """{branch name: (first parent, second parent)} of the latest merge whose subject names the branch."""
        if self._merges is None:
            self._merges = {}
            out = core.git(self.root, "log", "--all", "--merges", "--format=%P\x1f%s", check=False)
            for line in reversed(out.splitlines()):  # oldest first, so the latest merge wins
                parents, _, subj = line.partition("\x1f")
                ps = parents.split()
                m = re.match(r"Merge (?:remote-tracking )?branch '([^']+)'", subj) or re.match(r"Merge pull request #\d+ from \S+?/(\S+)", subj)
                if m and len(ps) >= 2:
                    self._merges[re.sub(r"^origin/", "", m.group(1))] = (ps[0], ps[1])
        return self._merges

    def gone_glob(self, cwd):
        p = os.path.normpath(cwd)
        while p and p != os.sep:
            if any(fnmatch.fnmatchcase(p, g) for g in self.globs):
                return True
            up = os.path.dirname(p)
            if up == p:   # a drive root such as D:\ is its own parent on Windows
                break
            p = up
        return False

    def owns(self, cwd, branch=None):
        """True when a record with this cwd (and Claude's gitBranch, if any) belongs to the repo. A cwd that
        no longer exists needs evidence: a non-generic branch known here, or a worktree_roots glob (which then
        stays unattributed unless the branch proves the repo)."""
        if self.member(cwd):
            return True
        if not cwd or not isinstance(cwd, str) or os.path.exists(cwd):
            return False
        if branch and branch in self.strong():
            return True
        return self.gone_glob(cwd)

    def member(self, cwd):
        """(worktree path, its current branch) when cwd is a worktree of the repo or inside one."""
        if not cwd or not isinstance(cwd, str):
            return None
        if cwd not in self._member:
            real = os.path.realpath(cwd)
            hit = None
            for p, b in self.worktrees:
                if real == p or real.startswith(p.rstrip(os.sep) + os.sep):
                    hit = (p, b)
                    break
            self._member[cwd] = hit
        return self._member[cwd]

    def branch_at(self, wt, ts, fallback):
        """Branch checked out in worktree `wt` at time `ts`, from its HEAD reflog; else the current one."""
        if wt not in self._reflog:
            rows = []
            out = core.git(wt, "reflog", "show", "--date=unix", "--format=%gd%x1f%gs", "HEAD", check=False)
            for line in out.splitlines():
                m = re.match(r"HEAD@\{(\d+)\}\x1fcheckout: moving from (.+) to (.+)$", line)
                if m:
                    rows.append((int(m.group(1)), m.group(2), m.group(3)))
            rows.sort()
            self._reflog[wt] = rows
        rows = self._reflog[wt]
        if not rows:
            return fallback
        times = [r[0] for r in rows]
        i = bisect.bisect_right(times, int(ts))
        name = rows[i - 1][2] if i else rows[0][1]
        return name

    def chain(self, branch):
        """[(sha, committer time)] oldest first along the first-parent line of a local branch."""
        if branch not in self._chain:
            rows = []
            args = None
            if branch in self.branches:
                args = ["refs/heads/" + branch]
            elif core.git(self.root, "rev-parse", "--verify", "-q", "refs/remotes/origin/" + branch, check=False).strip():
                args = ["refs/remotes/origin/" + branch]
            elif branch in self.merges():  # deleted but merged: the merge's second-parent side
                p1, p2 = self.merges()[branch]
                args = [p2, "^" + p1]
            if args:
                out = core.git(self.root, "log", "--first-parent", "--format=%H %ct", *args, check=False)
                for line in out.splitlines():
                    h, t = line.split()
                    rows.append((h, int(t)))
                rows.reverse()
            self._chain[branch] = rows
        return self._chain[branch]


# ---------- readers (each returns records and git-commit events) ----------
UNKNOWN_MODEL = "unknown model"


def _model(x):
    """A model name exactly as the tool wrote it, or '' (never guessed). Synthetic placeholders are not models."""
    x = x.strip() if isinstance(x, str) else ""
    return "" if not x or x.startswith("<") else x[:80]


def _rec(tool, ts, cwd, branch, session, i, cr, cw, o, model=""):
    return {"tool": tool, "ts": ts, "cwd": cwd, "branch": branch, "session": session,
            "input": i, "cache_read": cr, "cache_write": cw, "output": o, "model": model}


def _files(base, suffix, since_ts):
    for dp, dns, fns in os.walk(base):
        for fn in sorted(fns):
            if fn.endswith(suffix):
                p = os.path.join(dp, fn)
                try:
                    if since_ts is not None and os.path.getmtime(p) < since_ts:
                        continue
                except OSError:
                    continue
                yield p


def cache_path():
    return os.path.join(defaults.source("cache_dir"), "claude-index.json")


INDEX_VERSION = 2


def _load_index():
    try:
        with open(cache_path(), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") == INDEX_VERSION and isinstance(d.get("files"), dict):
            return d
    except (OSError, ValueError, AttributeError):
        pass
    return {"version": INDEX_VERSION, "files": {}}


def _save_index(idx):
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(idx, separators=(",", ":")))  # dumps (C encoder); dump() to a file is ~5x slower
        os.replace(tmp, path)
    except OSError:
        pass  # the index is only a speed-up


def _index_file(path, entry, stats):
    """Append-only transcript: parse only the bytes past `entry["offset"]`. Per assistant line the index keeps
    [ts, cwd, gitBranch, message id or '', model, input, cache_read, cache_write, output, byte offset of the line] and,
    for Bash `git commit` calls, [ts, cwd, gitBranch]. No message text is stored. Repo-independent."""
    try:
        size = os.path.getsize(path)
        f = open(path, "rb")
    except OSError:
        stats["unreadable"] += 1
        return entry
    if entry is None or size < entry["offset"]:
        entry = {"offset": 0, "recs": [], "ev": []}
    with f:
        f.seek(entry["offset"])
        pos = entry["offset"]
        for line in f:
            start, pos = pos, pos + len(line)
            if not line.endswith(b"\n"):
                pos = start  # a half-written last line: read it next time
                break
            if b'"assistant"' not in line:
                continue
            try:
                d0 = json.loads(line)
            except ValueError:
                stats["bad_lines"] += 1
                continue
            msg = d0.get("message") if isinstance(d0, dict) else None
            if d0.get("type") != "assistant" or not isinstance(msg, dict):
                continue
            ts = parse_ts(d0.get("timestamp"))
            if ts is None:
                stats["bad_lines"] += 1
                continue
            cwd, br = d0.get("cwd"), d0.get("gitBranch")
            content = msg.get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash":
                    cmd = (c.get("input") or {}).get("command")
                    if isinstance(cmd, str) and COMMIT_CALL.search(cmd):
                        entry["ev"].append([ts, cwd, br])
            u = msg.get("usage")
            if isinstance(u, dict):
                entry["recs"].append([ts, cwd, br, msg.get("id") or "", _model(msg.get("model")), _num(u.get("input_tokens")),
                                      _num(u.get("cache_read_input_tokens")), _num(u.get("cache_creation_input_tokens")),
                                      _num(u.get("output_tokens")), start])
    entry["offset"] = pos
    return entry


def read_claude(base, repo, since_ts, stats):
    recs, events = {}, {}
    if not os.path.isdir(base):
        return [], events
    idx, dirty = _load_index(), False
    for path in _files(base, ".jsonl", since_ts):
        e = idx["files"].get(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if e is None or e["offset"] != size:
            ne = _index_file(path, e, stats)
            if ne:
                idx["files"][path] = ne
                dirty = True
            e = ne
        if not e:
            continue
        owned = {}

        def owns(cwd, br):
            k = (cwd, br)
            if k not in owned:
                owned[k] = bool(repo.owns(cwd, br))
            return owned[k]
        for ts, cwd, br, mid, model, *vals, off in e["recs"]:
            if not owns(cwd, br):
                continue
            key = mid or "%s:%d" % (path, off)
            r = recs.get(key)
            if r is None:  # streamed duplicates and resumed-session copies share message.id
                recs[key] = _rec("claude", ts, cwd, br if br and br != "HEAD" else None, path, *vals, model=model)
            else:
                for k2, v in zip(FIELDS, vals):
                    r[k2] = max(r[k2], v)
                r["model"] = r["model"] or model
                r["ts"] = max(r["ts"], ts)
                stats["duplicates"] += 1
        for ts, cwd, br in e["ev"]:
            if owns(cwd, br):
                events.setdefault(path, []).append(ts)
    if dirty:
        _save_index(idx)
    return list(recs.values()), events


def read_codex(base, repo, since_ts, stats):
    recs, events = [], {}
    if not os.path.isdir(base):
        return recs, events
    for path in _files(base, ".jsonl", since_ts):
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            stats["unreadable"] += 1
            continue
        with f:
            cwd, prev, ok, model = None, dict.fromkeys(FIELDS, 0), None, ""
            for line in f:
                if ok is None:
                    try:
                        first = json.loads(line)
                    except ValueError:
                        break
                    p = first.get("payload") if isinstance(first, dict) else None
                    cwd = p.get("cwd") if isinstance(p, dict) and first.get("type") == "session_meta" else None
                    ok = bool(repo.owns(cwd))
                    if not ok:
                        break
                    continue
                is_call = '"function_call"' in line or "local_shell_call" in line
                if '"turn_context"' in line:
                    try:
                        tc = json.loads(line).get("payload")
                    except ValueError:
                        continue
                    if isinstance(tc, dict):
                        model = _model(tc.get("model")) or model
                    continue
                if '"token_count"' not in line and not is_call:
                    continue
                try:
                    d0 = json.loads(line)
                except ValueError:
                    stats["bad_lines"] += 1
                    continue
                p = d0.get("payload") if isinstance(d0, dict) else None
                ts = parse_ts(d0.get("timestamp")) if isinstance(d0, dict) else None
                if not isinstance(p, dict) or ts is None:
                    continue
                if is_call and p.get("type") in ("function_call", "local_shell_call"):
                    if COMMIT_CALL.search(json.dumps(p.get("arguments") or p.get("action") or "")):
                        events.setdefault(path, []).append(ts)
                    continue
                if p.get("type") != "token_count":
                    continue
                tot = (p.get("info") or {}).get("total_token_usage")
                if not isinstance(tot, dict):
                    continue
                cached = _num(tot.get("cached_input_tokens"))
                cur = {"input": max(0, _num(tot.get("input_tokens")) - cached), "cache_read": cached,
                       "cache_write": _num(tot.get("cache_write_input_tokens")), "output": _num(tot.get("output_tokens"))}
                if any(cur[k] < prev[k] for k in FIELDS):  # counter went back: new baseline, keep everything
                    delta = dict(cur)
                else:
                    delta = {k: cur[k] - prev[k] for k in FIELDS}
                prev = cur
                if any(delta.values()):
                    recs.append(_rec("codex", ts, cwd, None, path, *[delta[k] for k in FIELDS], model=model))
    return recs, events


def read_opencode(db, repo, since_ts, stats):
    recs = []
    if not os.path.isfile(db):
        return recs, {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        rows = con.execute("select id, session_id, time_created, data from message where time_created >= ? order by time_created",
                           (int((since_ts or 0) * 1000),)).fetchall()
        con.close()
    except sqlite3.Error:
        stats["unreadable"] += 1
        return recs, {}
    for mid, sid, tc, data in rows:
        try:
            d0 = json.loads(data)
        except (ValueError, TypeError):
            stats["bad_lines"] += 1
            continue
        if not isinstance(d0, dict) or d0.get("role") != "assistant":
            continue
        cwd = (d0.get("path") or {}).get("cwd")
        t = d0.get("tokens")
        if not repo.owns(cwd) or not isinstance(t, dict):
            continue
        ca = t.get("cache") if isinstance(t.get("cache"), dict) else {}
        done = (d0.get("time") or {}).get("completed")
        ts = parse_ts(done if isinstance(done, (int, float)) else tc)
        recs.append(_rec("opencode", ts, cwd, None, "opencode:" + str(sid), _num(t.get("input")), _num(ca.get("read")),
                         _num(ca.get("write")), _num(t.get("output")) + _num(t.get("reasoning")),
                         model=_model("/".join(x for x in (d0.get("providerID"), d0.get("modelID")) if isinstance(x, str) and x))))
    return recs, {}


# ---------- attribution ----------
def _boundaries(chain, events):
    """Per commit the time up to which a session's records belong to it: the commit time, or the
    session's own `git commit` tool call when one sits just before it. `events` is sorted."""
    used, out = set(), []
    for _, t in chain:
        eff = t + EPS
        i = bisect.bisect_left(events, t - WINDOW)
        while i < len(events) and events[i] <= t + 1:
            if i not in used:
                used.add(i)
                eff = min(events[i], eff)
                break
            i += 1
        out.append(max(eff, out[-1]) if out else eff)
    return out


def attribute(repo, records, events):
    """Return [(record, sha or None, branch or None, reason or None)]; every record exactly once."""
    out, cache = [], {}
    for r in records:
        wt, cur = repo.member(r["cwd"]) or (None, None)
        if wt is None:  # the directory is gone: only a branch known here can say where the record goes
            if r["branch"] and r["branch"] in repo.strong() and repo.chain(r["branch"]):
                branch = r["branch"]
            else:
                out.append((r, None, r["branch"], "gone-worktree"))
                continue
        else:
            branch = r["branch"] or repo.branch_at(wt, r["ts"], cur)
        if not branch:
            out.append((r, None, branch, "unknown-branch"))
            continue
        chain = repo.chain(branch)
        if not chain:
            out.append((r, None, branch, "unknown-branch"))
            continue
        ev = events.get(r["session"])
        k = (branch, r["session"] if ev else None)  # sessions without `git commit` calls share the plain commit times
        if k not in cache:
            cache[k] = _boundaries(chain, sorted(ev or ()))
        b = cache[k]
        i = bisect.bisect_left(b, r["ts"])
        if i >= len(b):
            out.append((r, None, branch, "after-last-commit"))
        else:
            out.append((r, chain[i][0], branch, None))
    return out


# ---------- API ----------
def _zero():
    z = dict.fromkeys(FIELDS, 0)
    z["messages"] = 0
    return z


def _add(tot, r):
    for k in FIELDS:
        tot[k] += r[k]
    tot["messages"] += 1


def configured_roots(root, config=None):
    path = config or os.path.join(root, "dropviz.yaml")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        v = core.miniyaml.loads(f.read()).get("worktree_roots") or []
    return [v] if isinstance(v, str) else list(v)


def collect(root, sources=None, since=None, worktree_roots=None, config=None):
    """All repo records attributed. {"rows": [...], "stats": {...}}."""
    repo = Repo(root, worktree_roots if worktree_roots is not None else configured_roots(root, config))
    src = dict(default_sources(), **(sources or {}))
    since_ts = parse_since(since) if isinstance(since, str) else since
    stats = {"unreadable": 0, "bad_lines": 0, "duplicates": 0}
    recs, events = [], {}
    for name, reader in (("claude", read_claude), ("codex", read_codex), ("opencode", read_opencode)):
        rs, ev = reader(src[name], repo, since_ts, stats)
        recs += rs
        events.update(ev)
    if since_ts is not None:
        recs = [r for r in recs if r["ts"] >= since_ts]
    return {"repo": repo, "rows": attribute(repo, recs, events), "stats": stats}


def tokens(root, sources=None, since=None, commit=None, branch=None, worktree_roots=None, config=None):
    c = collect(root, sources, since, worktree_roots, config)
    repo, rows = c["repo"], c["rows"]
    if commit:
        full = core.git(root, "rev-parse", "--verify", "-q", commit + "^{commit}", check=False).strip()
        if not full:
            raise ValueError(i18n.t(i18n.from_env(), "err.unknown_commit", commit=commit))
        rows = [x for x in rows if x[1] == full]
    if branch:
        rows = [x for x in rows if x[2] == branch or (x[2] is None and not commit)]
    commits, un, total, by_day = {}, {"by_tool": {}, "reasons": {}}, {}, {}
    by_model, commit_models = {}, {}
    for r, sha, br, reason in rows:
        t = r["tool"]
        day = datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0) + sum(r[k] for k in FIELDS)
        _add(total.setdefault(t, _zero()), r)
        mdl = r.get("model") or UNKNOWN_MODEL
        _add(by_model.setdefault(mdl, dict(_zero(), tool=t)), r)
        if sha:
            _add(commit_models.setdefault(sha, {}).setdefault(mdl, dict(_zero(), tool=t)), r)
            _add(commits.setdefault(sha, {}).setdefault(t, _zero()), r)
        else:
            _add(un["by_tool"].setdefault(t, _zero()), r)
            _add(un["reasons"].setdefault(reason, {}).setdefault(t, _zero()), r)
    return {"repo": repo.root, "since": since if isinstance(since, str) else None, "commit": commit,
            "branch": branch, "by_day": by_day, "commits": commits, "by_model": by_model, "commit_models": commit_models, "unattributed": un, "total": total, "stats": c["stats"],
            "note": "token counts only; unattributed = records in this repo that no commit window contains"}


_FS = {}


def _feature_shas(root, features):
    """{feature id: set of commits (any branch) touching its paths}; one git pass, cached per HEAD set."""
    key = (root, core.git(root, "rev-parse", "--all", check=False).strip().replace("\n", ","),
           tuple((f["id"], tuple(f.get("paths") or [])) for f in features))
    if key not in _FS:
        rows = core._log_files(root, "--branches", "--remotes", "--tags")
        _FS.clear()
        _FS[key] = {}
        for f in features:
            hit = core._matcher(f.get("paths") or [])
            _FS[key][f["id"]] = {r[0] for r in rows if any(hit(n) for n in r[4])} if f.get("paths") else set()
    return _FS[key]


def feature_tokens(root, result, features):
    """{feature id: {tool: counts}} summed over the commits touching the feature's paths. A commit
    that touches several features counts in each, so feature sums can exceed the total."""
    out = {}
    for f in features:
        paths = f.get("paths") or []
        if not paths:
            continue
        shas = _feature_shas(root, features)[f["id"]]
        acc = {}
        for sha in shas & set(result["commits"]):
            for t, c in result["commits"][sha].items():
                a = acc.setdefault(t, _zero())
                for k in FIELDS + ("messages",):
                    a[k] += c[k]
        out[f["id"]] = acc
    return out


def feature_models(root, result, features):
    """{feature id: {model: counts}} like feature_tokens, per exact model name ('unknown model' when not recorded)."""
    out = {}
    cm = result.get("commit_models") or {}
    for f in features:
        paths = f.get("paths") or []
        if not paths:
            continue
        shas = _feature_shas(root, features)[f["id"]]
        acc = {}
        for sha in shas & set(cm):
            for m, c in cm[sha].items():
                a = acc.setdefault(m, dict(_zero(), tool=c.get("tool", "")))
                for k in FIELDS + ("messages",):
                    a[k] += c[k]
        out[f["id"]] = acc
    return out


def _sum(c):
    return sum(c[k] for k in FIELDS)


def summary(result):
    """Totals for the big counter. Counts only. total = every record of the repo (attributed or not); days with no
    record are None in the series (a gap: not recorded), never zero."""
    per_tool = {t: dict((k, c[k]) for k in FIELDS) for t, c in result["total"].items()}
    parts = {k: sum(c[k] for c in per_tool.values()) for k in FIELDS}
    days = sorted(result["by_day"])
    series, run = [], 0
    if days:
        first_d = datetime.date.fromisoformat(days[0])
        last_d = datetime.date.fromisoformat(days[-1])
        d = first_d
        while d <= last_d:
            k = d.isoformat()
            if k in result["by_day"]:
                run += result["by_day"][k]
                series.append({"day": k, "cumulative": run})
            else:
                series.append({"day": k, "cumulative": None})  # no transcript survived for this day
            d += datetime.timedelta(days=1)
    return {"total": sum(parts.values()), "input": parts["input"], "output": parts["output"],
            "cache_read": parts["cache_read"], "cache_write": parts["cache_write"],
            "per_tool": {t: _sum(c) for t, c in per_tool.items()},
            "per_model": {m: _sum(c) for m, c in result.get("by_model", {}).items()}, "series": series}


def ranking(features, result, fmodels, min_features=None):
    """Per model: credit for verified-and-merged work. Each done feature is split equally among the models that
    recorded work on it; a done feature with no model record goes to 'no record'. So credits add up to the number of
    done features. tokens_per_feature = tokens of that model / its credit (counts only). Under min_features credit the
    figure is withheld ('enough' false)."""
    min_features = defaults.get("ranking_min_features") if min_features is None else min_features
    done = [f for f in features if f["status"] == "done"]
    credit, toks = {}, {}
    for f in done:
        ms = fmodels.get(f["id"]) or {}
        if not ms:
            credit[None] = credit.get(None, 0) + 1
            continue
        for m in ms:
            credit[m] = credit.get(m, 0) + 1.0 / len(ms)
    for m, c in result.get("by_model", {}).items():
        toks[m] = _sum(c)
    rows = []
    for m in set(credit) | set(toks):
        cr = credit.get(m, 0)
        rows.append({"model": m, "credit": round(cr, 4), "share": (cr / len(done)) if done else 0.0, "tokens": toks.get(m, 0),
                     "tokens_per_feature": (toks.get(m, 0) / cr) if cr >= min_features and m else None,
                     "enough": cr >= min_features})
    rows.sort(key=lambda r: (-r["credit"], str(r["model"])))
    return {"done_features": len(done), "rows": rows, "min_features": min_features}


def write_notes(root, result):
    """Write per-commit totals into refs/notes/dropviz-tokens. Only called when the user asks."""
    ident = []
    if not core.git(root, "config", "user.email", check=False).strip():
        ident = ["-c", "user.name=dropviz", "-c", "user.email=dropviz" + chr(64) + "localhost"]
    n = 0
    for sha, per in result["commits"].items():
        text = "\n".join("%s input=%d cache_read=%d cache_write=%d output=%d" % (
            t, c["input"], c["cache_read"], c["cache_write"], c["output"]) for t, c in sorted(per.items()))
        core.git(root, *(ident + ["notes", "--ref=" + NOTES_REF, "add", "-f", "-m", text, sha]))
        n += 1
    return n


def format_text(root, res):
    L = i18n.from_env()
    tt = lambda k, **kw: i18n.t(L, "tokens." + k, **kw)
    subj = {}
    for line in core.git(root, "log", "--branches", "--remotes", "--tags", "--format=%H\x1f%s", check=False).splitlines():
        h, _, s = line.partition("\x1f")
        subj[h] = s
    head = "%-10s %-9s %12s %12s %12s %12s" % (tt("col_commit"), tt("col_tool"), tt("col_input"), tt("col_cache_read"),
                                                tt("col_cache_write"), tt("col_output"))
    lines = [head]

    def row(label, t, c, tail=""):
        return "%-10s %-9s %12d %12d %12d %12d %s" % (label, t, c["input"], c["cache_read"], c["cache_write"], c["output"], tail)
    order = sorted(res["commits"], key=lambda s: -sum(c["input"] + c["output"] for c in res["commits"][s].values()))
    for sha in order:
        for t in sorted(res["commits"][sha]):
            lines.append(row(sha[:8], t, res["commits"][sha][t], subj.get(sha, "")[:50]))
    for reason, per in sorted(res["unattributed"]["reasons"].items()):
        for t in sorted(per):
            lines.append(row(tt("unattributed"), t, per[t], "(%s)" % reason))
    for t in sorted(res["total"]):
        lines.append(row(tt("total"), t, res["total"][t]))
    if not res["total"]:
        lines.append(tt("none"))
    st = res["stats"]
    lines.append(tt("stats", dup=st["duplicates"], unread=st["unreadable"], bad=st["bad_lines"]))
    return "\n".join(lines)

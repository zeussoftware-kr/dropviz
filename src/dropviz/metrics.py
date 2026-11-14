"""Headline number: verified and merged work, per day and per week.

A unit of work is one commit on the default branch's first-parent line (a merge, or a direct commit).
It counts as *verified* only when a verification record exists at or after it on that line:
a git note `pass` (refs/notes/dropviz-verify, no `feature=` scope), or a `pass` entry for the commit in
the optional local file `.dropviz/ci-status.json` ({"<sha>": "pass" | "fail"}). A `fail` entry for the
unit's own commit overrides. Unverified merged work is shown separately, never mixed in.
Tokens (counts only) are shown next to it when a tokens result is given. No money anywhere."""
import datetime
import json
import os
import re
import time

from . import core, defaults, i18n

DAY = 24 * 3600
WEEK = 7 * DAY


def _m(lang, key, **kw):
    return i18n.t(lang or i18n.from_env(), "metrics." + key, **kw)


def _day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _week(ts):
    y, w, _ = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isocalendar()
    return "%d-W%02d" % (y, w)


def ci_status(root):
    try:
        with open(os.path.join(root, ".dropviz", "ci-status.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return {str(k): str(v).lower() for k, v in d.items()} if isinstance(d, dict) else {}


def _units(root, default):
    """[(sha, committer time)] oldest first along the default branch's first-parent line."""
    rows = []
    for line in core.git(root, "log", default, "--first-parent", "--format=%H %ct", check=False).splitlines():
        h, t = line.split()
        rows.append((h, int(t)))
    rows.reverse()
    return rows


def unit_positions(root, default):
    """{commit: index of the first-parent unit that brought it into the default branch} in one git call."""
    rows = [l.split() for l in core.git(root, "log", "--topo-order", "--parents", "--format=%H %P", default, check=False).splitlines()]
    par = {r[0]: r[1:] for r in rows}
    chain, c = [], rows[0][0] if rows else None
    while c:
        chain.append(c)
        c = par[c][0] if par[c] else None
    chain.reverse()
    out = {}
    for i, u in enumerate(chain):
        stack = [u]
        while stack:
            x = stack.pop()
            if x in out or x not in par:
                continue
            out[x] = i
            stack.extend(par[x])
    return out


def _position(root, default, pos, sha, allpos=None):
    """Index of the first-parent unit that first contains `sha` (None when it is not on the default branch)."""
    if allpos is not None and sha in allpos:
        return allpos[sha]
    full = core.git(root, "rev-parse", "--verify", "-q", sha + "^{commit}", check=False).strip()
    if not full:
        return None
    if allpos is not None and full in allpos:
        return allpos[full]
    if full in pos:
        return pos[full]
    if not core._is_ancestor(root, full, default):
        return None
    out = core.git(root, "log", "--first-parent", "--format=%H", "--ancestry-path", "%s..%s" % (full, default),
                   check=False).split()
    return pos.get(out[-1]) if out else None


def verified_flags(root, default, units):
    """-> (flags per unit, scoped) ; scoped = {feature id: [flag per unit]} built from `pass feature=x` notes."""
    pos = {h: i for i, (h, _) in enumerate(units)}
    allpos = unit_positions(root, default)
    n = len(units)
    best = -1                      # latest unscoped verification position
    scoped_best = {}               # feature -> latest position
    for sha, feats in core._verifications(root).items():
        p = _position(root, default, pos, sha, allpos)
        if p is None:
            continue
        if feats is None:
            best = max(best, p)
        else:
            for f in feats:
                scoped_best[f] = max(scoped_best.get(f, -1), p)
    ci = ci_status(root)
    fails = set()
    for sha, v in ci.items():
        p = _position(root, default, pos, sha, allpos) if v in ("pass", "fail") else None
        if p is None:
            continue
        if v == "pass":
            best = max(best, p)
        elif units[p][0] == core.git(root, "rev-parse", "--verify", "-q", sha + "^{commit}", check=False).strip():
            fails.add(p)
    flags = [i <= best and i not in fails for i in range(n)]
    scoped = {f: [i <= b and i not in fails for i in range(n)] for f, b in scoped_best.items()}
    return flags, scoped


def _eta(root, default, units, flags, scoped, feat, now, lang=None, cfg=None, unit_rows=None, unit_hits=None):
    min_points, window = defaults.get("eta_min_points", cfg), defaults.get("eta_window_weeks", cfg)
    paths = feat.get("paths") or []
    target = feat.get("target")
    out = {"feature": feat["id"], "done_items": 0, "target": target if isinstance(target, int) else None,
           "eta": None, "why": ""}
    if not paths:
        out["why"] = _m(lang, "no_paths")
        return out
    idx = {h: i for i, (h, _) in enumerate(units)}
    sf = scoped.get(feat["id"])
    weeks = {}
    for h, t in (unit_rows or []):
        if h not in unit_hits:
            continue
        i = idx.get(h)
        if i is not None and (flags[i] or (sf and sf[i])):
            out["done_items"] += 1
            weeks[_week(int(t))] = weeks.get(_week(int(t)), 0) + 1
    if out["target"] is None:
        out["why"] = _m(lang, "no_target")
    elif out["done_items"] >= out["target"]:
        out["why"] = _m(lang, "target_reached")
    elif len(weeks) < min_points:
        out["why"] = _m(lang, "not_enough", have=len(weeks), need=min_points)
    else:
        recent = [_week(now - k * WEEK) for k in range(window)]
        rate = sum(weeks.get(w, 0) for w in recent) / float(window)
        if rate <= 0:
            out["why"] = _m(lang, "none_recent", weeks=window)
        else:
            left = out["target"] - out["done_items"]
            out["eta"] = _day(now + left / rate * WEEK)
            out["why"] = _m(lang, "eta_rate", left=left, rate="%.1f" % rate, weeks=window)
    out["weekly"] = weeks
    return out


def compute(root, tokens_result=None, now=None, days=14, weeks=8, lang=None):
    now = int(time.time() if now is None else now)
    cfg = core.load_config(root)
    default = core.default_branch(root, cfg.get("default_branch"))
    units = _units(root, default) if core.git(root, "rev-parse", "--verify", "-q", default, check=False).strip() else []
    flags, scoped = verified_flags(root, default, units)
    urows, uhits = [], {}
    if units:  # files changed by each first-parent unit (a merge against its first parent), one git call
        rows = core._log_files(root, default, "--first-parent", "-m")
        urows = [(r[0], r[1]) for r in rows]
        feats = [f for f in cfg.get("features") or [] if isinstance(f, dict) and "id" in f]
        fp = [[f["paths"]] if isinstance(f.get("paths"), str) else list(f.get("paths") or []) for f in feats]
        for f, b in zip(feats, core._bucket(rows, fp)):
            uhits[f["id"]] = {r[0] for r in b}
    by_day, by_week = {}, {}
    for (h, t), ok in zip(units, flags):
        for table, key in ((by_day, _day(t)), (by_week, _week(t))):
            b = table.setdefault(key, {"verified": 0, "unverified": 0})
            b["verified" if ok else "unverified"] += 1
    tok_day = (tokens_result or {}).get("by_day") or {}
    tok_week = {}
    for d, v in tok_day.items():
        dt = datetime.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        tok_week[_week(dt.timestamp())] = tok_week.get(_week(dt.timestamp()), 0) + v
    day_keys = [_day(now - k * 86400) for k in range(days - 1, -1, -1)]
    week_keys = [_week(now - k * WEEK) for k in range(weeks - 1, -1, -1)]
    res = {
        "repo": root, "default_branch": default,
        "headline": {"verified_today": by_day.get(day_keys[-1], {}).get("verified", 0),
                     "verified_this_week": by_week.get(week_keys[-1], {}).get("verified", 0),
                     "verified_total": sum(flags), "merged_total": len(units)},
        "rule": _m(lang, "rule"),
        "days": [dict(day=k, **by_day.get(k, {"verified": 0, "unverified": 0}), tokens=tok_day.get(k)) for k in day_keys],
        "weeks": [dict(week=k, **by_week.get(k, {"verified": 0, "unverified": 0}), tokens=tok_week.get(k)) for k in week_keys],
        "tokens_note": _m(lang, "tokens_note"),
        "ci_status_file": bool(ci_status(root)),
        "eta": [_eta(root, default, units, flags, scoped, f, now, lang, cfg, urows, uhits.get(f["id"], ())) for f in cfg.get("features") or []
                if isinstance(f, dict) and "id" in f],
    }
    return res


def format_text(m, lang=None):
    h = m["headline"]
    lines = [_m(lang, "headline", today=h["verified_today"], week=h["verified_this_week"], ok=h["verified_total"],
                total=h["merged_total"], branch=m["default_branch"]),
             "", "%-10s %8s %10s %14s" % (_m(lang, "col_week"), _m(lang, "col_verified"), _m(lang, "col_unverified"), _m(lang, "col_tokens"))]
    for w in m["weeks"]:
        lines.append("%-10s %8d %10d %14s" % (w["week"], w["verified"], w["unverified"],
                                                 "-" if w["tokens"] is None else "{:,}".format(w["tokens"])))
    lines += ["", _m(lang, "eta_title")]
    for e in m["eta"]:
        lines.append("  %-28s %-10s %s" % (e["feature"], e["eta"] or "-", e["why"]))
    return "\n".join(lines)

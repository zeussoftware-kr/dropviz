"""Which AI model worked where, read from the commit messages of the default branch.

Sources: `Co-Authored-By` / `Co-Developed-By` trailers that name an AI tool or model, `Assisted-By` / `Generated-By`
trailers, and "Generated with ..." lines. Only the NAME is kept: e-mail addresses in a trailer are dropped and never
shown. A model name goes through a small documented table (defaults.json, `trailer.models`; see docs/TRAILERS.md);
a name the table does not know keeps its own words.

Counting rule: a commit counts once for each model named on it; its changed lines are shared equally between
those models, so shares add up. Several features touched by one commit each see that commit (as with tokens).
Nothing here is a measurement of tokens: tokens stay unknown unless local records exist."""
import re

from . import core, defaults

T = defaults.data()["trailer"]
KEY_RE = re.compile(r"^\s*(%s)\s*:\s*(.+?)\s*$" % "|".join(re.escape(k) for k in T["keys"]), re.I | re.M)
GEN_RE = re.compile(T["generated_with"], re.I | re.M)
TABLE = [(re.compile(e["re"], re.I), e["name"]) for e in T["models"]]


def _clean(name):
    name = re.sub(r"\((?:[^()]*)\)", " ", name)            # "(1M context)", "(via X)"
    name = re.sub(r"[\[\]<>\"']", " ", name)
    name = re.sub(r"(\d)[_\-](\d)", r"\1.\2", name)           # 4-5 -> 4.5
    name = re.sub(r"[\s_]+", " ", name).strip(" -:")
    words = [w.capitalize() if w.isalpha() and (w.islower() or (w.isupper() and len(w) > 3)) else w for w in name.split(" ")]
    return " ".join(words)


def model_name(raw):
    """A display name for the model/tool named by `raw` (no e-mail ever), or None when it is not a plain name."""
    raw = re.sub(r"<[^>]*>", " ", raw or "")
    if "@" in raw:
        return None
    raw = raw.strip()
    for rx, name in TABLE:
        m = rx.fullmatch(re.sub(r"\([^()]*\)", " ", raw).strip())
        if m:
            return _clean(m.expand(name)) or None
    return _clean(raw) or None


def _is_ai_name(name_raw, email):
    plain = re.sub(r"\([^()]*\)", " ", name_raw).strip()
    return bool(core._tool_for(name_raw, email) or any(rx.fullmatch(plain) for rx, _ in TABLE))


def models_in_message(body):
    """Model names named in the trailers of one commit message, in order, each once."""
    found = []
    for m in KEY_RE.finditer(body or ""):
        key, val = m.group(1).lower(), m.group(2)
        em = re.search(r"<([^>]*)>", val)
        email = em.group(1) if em else ""
        raw = re.sub(r"<[^>]*>", "", val).strip()
        # Assisted-by may read "agent:model [tools]"
        if key in T["always_ai_keys"]:
            raw = raw.split("[")[0].strip()
            if re.match(r"^[\w.\-]+:[\w.\-]+$", raw):
                raw = raw.split(":", 1)[1]
            ok = bool(raw)
        else:
            ok = _is_ai_name(raw, email)
        name = model_name(raw) if ok else None
        if name and name not in found:
            found.append(name)
    if not found:
        for m in GEN_RE.finditer(body or ""):
            name = model_name(m.group(1))
            if name and name not in found:
                found.append(name)
    return found


def collect(root, default):
    """[(sha, commit time, [models], {path: changed lines})] for commits of the default branch that name a model,
    newest first. One git call."""
    out = core.git(root, "log", default, "-i", "-E", "--grep=" + T["grep"], "--numstat", "--no-renames",
                   "--format=%x01%H%x02%ct%x02%B%x03", check=False)
    rows = []
    for rec in out.split("\x01")[1:]:
        head, _, stats = rec.partition("\x03")
        h, ct, body = (head.split("\x02") + ["", ""])[:3]
        ms = models_in_message(body)
        if not ms:
            continue
        files = {}
        for line in stats.split("\n"):
            parts = line.split("\t")
            if len(parts) == 3:
                a, d, path = parts
                files[path] = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
        rows.append((h, int(ct) if ct.isdigit() else 0, ms, files))
    return rows


def _entry():
    return {"commits": 0, "lines": 0.0, "first": None, "last": None}


def _add(e, ct, lines):
    e["commits"] += 1
    e["lines"] += lines
    e["first"] = ct if e["first"] is None else min(e["first"], ct)
    e["last"] = ct if e["last"] is None else max(e["last"], ct)


def summarize(root, state, until=None, rows=None):
    """-> {"by_model": {model: entry}, "features": {id: {model: entry}}, "areas": {area name: {model: entry}},
    "commits_total": n, "commits_with_model": n, "rows": rows}. Entry = {commits, lines, first, last} (times in
    epoch seconds). `until` limits to commits up to that time (the replay)."""
    rows = collect(root, state["default_branch"]) if rows is None else rows
    use = [r for r in rows if until is None or r[1] <= until]
    feats = state["features"]
    buckets = core._bucket([(r[0], r[1], "", "", list(r[3])) for r in use], [f["paths"] for f in feats])
    by_row = {r[0]: r for r in use}
    out_f, by_model, seen = {}, {}, set()
    for f, bucket in zip(feats, buckets):
        hit = core._matcher(f["paths"])
        acc = {}
        for b in bucket:
            sha, ct, ms, files = by_row[b[0]]
            lines = sum(n for p, n in files.items() if hit(p)) / float(len(ms))
            for m in ms:
                _add(acc.setdefault(m, _entry()), ct, lines)
        if acc:
            out_f[f["id"]] = acc
    for r in use:
        for m in r[2]:
            _add(by_model.setdefault(m, _entry()), r[1], sum(r[3].values()) / float(len(r[2])))
    areas = {}

    def walk(nodes):
        for a in nodes:
            ids = set()

            def gather(n):
                ids.update(n["features"])
                for k in n["children"]:
                    gather(k)
            gather(a)
            acc = {}
            for fid in ids:
                for m, e in (out_f.get(fid) or {}).items():
                    x = acc.setdefault(m, _entry())
                    x["commits"] = max(x["commits"], e["commits"])   # features overlap: never above the largest one
                    x["lines"] += e["lines"]
                    x["first"] = e["first"] if x["first"] is None else min(x["first"], e["first"])
                    x["last"] = e["last"] if x["last"] is None else max(x["last"], e["last"])
            if acc:
                areas[a["name"]] = acc
            walk(a["children"])
    walk(state["areas"])
    total = len(core.git(root, "rev-list", state["default_branch"], check=False).split()) if until is None else None
    return {"by_model": by_model, "features": out_f, "areas": areas, "commits_total": total,
            "commits_with_model": len(use), "rows": rows}


def colors(summary):
    """{model: colour} by rank of changed lines; models past the legend size share one 'others' colour."""
    ranked = sorted(summary["by_model"], key=lambda m: (-summary["by_model"][m]["lines"], m))
    pal = defaults.data()["model_palette"]
    other = defaults.data()["view_colors"]["models_other"]
    return {m: (pal[i] if i < min(len(pal), T["max_legend"]) else other) for i, m in enumerate(ranked)}


def shares(entries):
    """[(model, fraction)] biggest first, from {model: entry}; lines decide, commits when no line counts."""
    key = "lines" if sum(e["lines"] for e in entries.values()) > 0 else "commits"
    tot = sum(e[key] for e in entries.values()) or 1.0
    return sorted(((m, e[key] / tot) for m, e in entries.items()), key=lambda x: (-x[1], x[0]))

"""Read a local git repo (read-only) and compute honest feature status.

Honesty rule: a feature is `done` only when (1) commits touching its paths are merged into the
default branch AND (2) a verification record exists for such a commit. Anything else is
`in-progress` (unmerged work exists) or `unknown`. A claim or a report never changes status."""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import time

from . import defaults, donerule, i18n, miniyaml

NOTES_REF = "dropviz-verify"
CLAIM_TTL = int(defaults.get("claim_ttl_hours") * 3600)
DEFAULT_AI_PREFIXES = defaults.get("ai_branch_prefixes")
SKIP_DIRS = set(defaults.data()["skip_dirs"])


class GitError(Exception):
    pass


def git(repo, *args, check=True):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    p = subprocess.run(["git", "-c", "core.quotepath=off", "-C", repo] + list(args),
                       capture_output=True, env=env)
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise GitError(p.stderr.decode("utf-8", "replace").strip() or i18n.t(i18n.from_env(), "err.git_failed"))
    return out if p.returncode == 0 else ""


def repo_root(path):
    return git(path, "rev-parse", "--show-toplevel").strip()


def default_branch(repo, configured=None):
    if configured:
        return configured
    ref = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if ref:
        return ref
    for cand in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + cand, check=False).strip():
            return cand
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip() or "HEAD"


# ---------- AI-tool detection ----------
# Known tools. A trailer matches by e-mail domain, or by a name that starts with the tool name
# and continues only with model/product words (so "Claude Monet" is a human, "Claude Sonnet 5" a tool).
TOOLS = {k: (v["label"], tuple(v["domains"]), v["name"]) for k, v in defaults.data()["tools"].items()}
MODEL_WORDS = defaults.data()["model_words"]
TRAILER_RE = re.compile(r"^\s*co-authored-by:\s*(.+?)\s*$", re.I | re.M)


def _tool_for(name, email):
    name_l, email_l = name.strip().lower(), email.strip().lower()
    local, _, domain = email_l.partition("@")
    # a tool domain only counts for bot-style addresses: a person at that company is still a person
    if local not in defaults.data()["bot_locals"]:
        domain = ""
    for key, (label, domains, name_re) in TOOLS.items():
        # google.com is far too broad for a human address: require the name as well.
        if domain and domain in domains and key not in defaults.data()["name_required_tools"]:
            return label
        if re.fullmatch(r"(?:%s)(?:[\s\-_]+%s)*(?:\s*\(.*\))?" % (name_re, MODEL_WORDS), name_l):
            return label
    return None


def ai_tools_in_message(body):
    """Tool labels named in Co-Authored-By trailers of a commit message (body text is ignored)."""
    found = []
    for m in TRAILER_RE.finditer(body):
        val = m.group(1)
        em = re.search(r"<([^>]*)>", val)
        email = em.group(1) if em else ""
        name = re.sub(r"<[^>]*>", "", val).strip()
        tool = _tool_for(name, email)
        if tool and tool not in found:
            found.append(tool)
    return found


def classify(body, branches, ai_prefixes):
    """-> (kind 'human'|'ai', [tools], why). Trailers win; branch prefixes are the fallback."""
    tools = ai_tools_in_message(body)
    if tools:
        return "ai", tools, "trailer"
    for b in branches:
        for pre in ai_prefixes:
            if pre and b.startswith(pre):
                return "ai", [pre.rstrip("/")], "branch:" + pre
    return "human", [], "no AI trailer or AI branch prefix"


# ---------- config ----------
def load_config(root):
    path = os.path.join(root, "dropviz.yaml")
    if not os.path.isfile(path):
        path = os.path.join(root, defaults.data()["sources"]["state_dir"], "auto.yaml")  # a draft made for a first look
        if not os.path.isfile(path):
            return {}
    with open(path, encoding="utf-8") as f:
        return miniyaml.loads(f.read())


def _err(key, **kw):
    return i18n.t(i18n.from_env(), "err." + key, **kw)


def draft_parent_areas(areas, answer=""):
    """Group top-level folders under a few parent areas (from the folder names, named after what the user says they
    build). Returns [] when everything lands in one group: then the folders stay top-level areas."""
    d = defaults.data()
    words = set(re.findall(r"\w+", (answer or "").lower()))
    names = next((k["names"] for k in d["init_kinds"] if words & set(k["words"])), d["init_default_names"])
    roles = d["init_roles"]
    buckets = {0: [], 1: [], 2: [], 3: []}
    for a in areas:
        low = a.lower()
        i = 1 if low in roles["client"] else 2 if low in roles["server"] else 3 if low in roles["extra"] else 0
        buckets[i].append(a)
    used = [i for i in buckets if buckets[i]]
    return [(names[i], buckets[i]) for i in used] if len(used) > 1 else []


def draft_features(root):
    feats, seen = [], set()

    def add(fid, name, paths, why, area):
        base, n = fid, 2
        while fid in seen:
            fid = "%s-%d" % (base, n)
            n += 1
        seen.add(fid)
        f = {"id": fid, "name": name, "area": area}
        f.update({"paths": paths, "accept": why, "status": "unknown"})
        feats.append(f)

    slug = lambda t: re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    tracked = git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD", check=False).split("\0")
    tops = sorted({p.split("/", 1)[0] for p in tracked if "/" in p})
    for d in tops:
        if d.startswith(".") or d in SKIP_DIRS:
            continue
        subs = sorted({p.split("/")[1] for p in tracked if p.startswith(d + "/") and p.count("/") >= 2})[:40]
        if len(subs) >= 2:
            for sd in subs:
                add(slug(d + "-" + sd) or "area", d + "/" + sd, [d + "/" + sd + "/"],
                    "Draft from folder; write the acceptance criteria.", d)
        else:
            add(slug(d) or "area", d, [d + "/"], "Draft from top-level folder; write the acceptance criteria.", d)
    readme = next((p for p in tracked if p.lower() == "readme.md"), None)
    if readme:
        incode = False
        for line in git(root, "show", "HEAD:" + readme, check=False).splitlines():
            if line.startswith("```"):
                incode = not incode
            m = None if incode else re.match(r"^##\s+(.+?)\s*#*\s*$", line)
            if m:
                title = m.group(1)
                add("readme-" + (slug(title) or "section"), title, [],
                    "Draft from README heading; add paths so evidence can be found.", "README")
    return feats


def write_auto_config(root):
    """A first-look draft for a repo without dropviz.yaml, kept in the git-ignored state folder (never in the user's tree)."""
    cache = os.path.join(root, defaults.data()["sources"]["state_dir"])
    os.makedirs(cache, exist_ok=True)
    gi = os.path.join(cache, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*\n")
    feats = draft_features(root)
    cfg = {"name": os.path.basename(root.rstrip("/")), "default_branch": default_branch(root),
           "ai_branch_prefixes": defaults.get("ai_branch_prefixes"), "features": feats}
    areas = list(dict.fromkeys(f["area"] for f in feats))
    groups = draft_parent_areas(areas)
    cfg["areas"] = ([x for parent, kids in groups for x in [{"name": parent}] + [{"name": k, "parent": parent} for k in kids]]
                    if groups else [{"name": k} for k in areas])
    path = os.path.join(cache, "auto.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(miniyaml.dumps_config(cfg))
    return path


def write_draft(root, force=False, answer=""):
    """Write dropviz.yaml draft + .dropviz/ cache dir. Returns (path, written)."""
    path = os.path.join(root, "dropviz.yaml")
    cache = os.path.join(root, ".dropviz")
    os.makedirs(cache, exist_ok=True)
    gi = os.path.join(cache, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*\n")
    if os.path.exists(path) and not force:
        return path, False
    feats = draft_features(root)
    cfg = {"name": os.path.basename(root), "default_branch": default_branch(root),
           "ai_branch_prefixes": DEFAULT_AI_PREFIXES, "features": feats}
    areas = list(dict.fromkeys(f["area"] for f in feats))
    cfg["areas"] = []
    groups = draft_parent_areas(areas, answer)
    if groups:
        for parent, kids in groups:
            cfg["areas"].append({"name": parent})
            cfg["areas"] += [{"name": k, "parent": parent} for k in kids]
    else:
        cfg["areas"] = [{"name": k} for k in areas]
    with open(path, "w", encoding="utf-8") as f:
        f.write(miniyaml.dumps_config(cfg))
    return path, True


# ---------- claims / reports (.dropviz/*.json) ----------
def _cache(root):
    d = os.path.join(root, ".dropviz")
    os.makedirs(d, exist_ok=True)
    return d


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


class _Lock:
    def __init__(self, root):
        self.path = os.path.join(_cache(root), "lock")

    def __enter__(self):
        end = time.time() + 5
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 10:
                        os.unlink(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise RuntimeError(_err("lock"))
                time.sleep(0.02)

    def __exit__(self, *a):
        os.close(self.fd)
        try:
            os.unlink(self.path)
        except OSError:
            pass


def live_claims(root, now=None):
    now = time.time() if now is None else now
    claims = _read_json(os.path.join(_cache(root), "claims.json"), {})
    return {k: v for k, v in claims.items() if now - v.get("last", 0) < CLAIM_TTL}


def claim(root, feature, session, note="", now=None):
    now = time.time() if now is None else now
    path = os.path.join(_cache(root), "claims.json")
    with _Lock(root):
        claims = live_claims(root, now)
        cur = claims.get(feature)
        if cur and cur["session"] != session:
            return False, cur
        claims[feature] = {"session": session, "since": cur["since"] if cur else now,
                           "last": now, "note": note}
        _write_json(path, claims)
        return True, claims[feature]


def report(root, feature, session, note="", now=None):
    now = time.time() if now is None else now
    path = os.path.join(_cache(root), "reports.json")
    with _Lock(root):
        reps = _read_json(path, [])
        reps.append({"feature": feature, "session": session, "time": now, "note": note, "claim": "done"})
        _write_json(path, reps)
        # a report counts as activity for the session's own claim
        cpath = os.path.join(_cache(root), "claims.json")
        claims = live_claims(root, now)
        if feature in claims and claims[feature]["session"] == session:
            claims[feature]["last"] = now
            _write_json(cpath, claims)
    return reps[-1]


# ---------- attention: local counters, never sent anywhere ----------
def bump_attention(root, feature, kind):
    """Count one local event for a feature: kind 'mcp' (an assistant asked about it) or 'opens' (detail opened in the page)."""
    if kind not in ("mcp", "opens"):
        return
    path = os.path.join(_cache(root), "attention.json")
    with _Lock(root):
        d = _read_json(path, {})
        if not isinstance(d, dict):
            d = {}
        e = d.setdefault(feature, {"mcp": 0, "opens": 0})
        e[kind] = e.get(kind, 0) + 1
        _write_json(path, d)


def attention(root):
    d = _read_json(os.path.join(_cache(root), "attention.json"), {})
    return d if isinstance(d, dict) else {}


# ---------- state ----------
def _branches(root, default):
    out = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes", check=False)
    return [b for b in out.split() if b != default and not b.endswith("/HEAD") and b != "origin/" + default]


def _verification_notes(root):
    """{commit: note text} for every note in refs/notes/dropviz-verify (one list call, one cat-file call)."""
    pairs = [l.split() for l in git(root, "notes", "--ref=" + NOTES_REF, "list", check=False).splitlines()]
    pairs = [p for p in pairs if len(p) == 2]
    if not pairs:
        return {}
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    p = subprocess.run(["git", "-C", root, "cat-file", "--batch"], input=("\n".join(b for b, _ in pairs) + "\n").encode(),
                       capture_output=True, env=env)
    data, pos, out = p.stdout, 0, {}
    for blob, commit in pairs:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        head = data[pos:nl].split()
        if len(head) < 3 or head[1] != b"blob":
            pos = nl + 1
            continue
        size = int(head[2])
        out[commit] = data[nl + 1:nl + 1 + size].decode("utf-8", "replace").strip()
        pos = nl + 1 + size + 1
    return out


def _verifications(root, notes=None):
    """{commit: [feature ids] or None(all)} from git notes `pass` records."""
    res = {}
    for sha, text in (notes if notes is not None else _verification_notes(root)).items():
        first = text.splitlines()[0].strip().lower() if text else ""
        if first.split()[:1] == ["pass"]:
            feats = re.findall(r"feature=([\w.\-]+)", text)
            res[sha] = feats or None
    return res


def _is_ancestor(root, a, b):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", a, b],
                          capture_output=True, env=env).returncode == 0


def _matcher(paths):
    """git-pathspec-like test for a file name: `dir/` or `dir` or a file = prefix, `*`/`?` = glob."""
    exact, globs = [], []
    for p in paths:
        p = str(p).strip()
        if not p:
            continue
        (globs if any(c in p for c in "*?[") else exact).append(p)
    pre = tuple(p.rstrip("/") for p in exact)

    def match(name):
        for p in pre:
            if name == p or name.startswith(p + "/"):
                return True
        return any(fnmatch.fnmatchcase(name, g) for g in globs)
    return match


def _log_files(root, *rng):
    """[(sha, ct, subject, body, [files])] newest first for `git log rng` (merges carry no files)."""
    out = git(root, "log", *rng, "--name-only", "--no-renames", "--format=%x01%H%x02%ct%x02%s%x02%B%x03", check=False)
    rows = []
    for rec in out.split("\x01")[1:]:
        head, _, names = rec.partition("\x03")
        h, ct, subj, body = (head.split("\x02") + ["", "", ""])[:4]
        rows.append((h, int(ct) if ct.isdigit() else 0, subj, body, [n for n in names.split("\n") if n]))
    return rows


def _verify_reach(root, default, verif):
    """{commit on default: (sha of an unscoped pass at/after it, {feature: sha of a scoped pass at/after it})}.
    One pass over the default branch's ancestry graph instead of one git call per feature and note."""
    rows = [l.split() for l in git(root, "rev-list", "--topo-order", "--parents", default, check=False).splitlines()]
    kids = {}
    for r in rows:
        for par in r[1:]:
            kids.setdefault(par, []).append(r[0])
    info = {}
    for r in rows:  # children come before their parents in --topo-order
        c = r[0]
        anyv, feat = None, {}
        if c in verif:
            if verif[c] is None:
                anyv = c
            else:
                for f in verif[c]:
                    feat[f] = c
        for k in kids.get(c, ()):
            ka, kf = info[k]
            anyv = anyv or ka
            for f, v in kf.items():
                feat.setdefault(f, v)
        info[c] = (anyv, feat)
    return info


def _bucket(rows, matchers_paths):
    """For each feature (list of paths) the rows (newest first) that touch it, in one pass over the files.
    Directory/file paths are looked up by prefix; glob paths are tested directly."""
    idx, globs, out = {}, [], [[] for _ in matchers_paths]
    for i, paths in enumerate(matchers_paths):
        for p in paths:
            p = str(p).strip()
            if not p:
                continue
            if any(c in p for c in "*?["):
                globs.append((i, p))
            else:
                idx.setdefault(p.rstrip("/"), []).append(i)
    for r in rows:
        hit = set()
        for name in r[4]:
            parts = name.split("/")
            for k in range(1, len(parts) + 1):
                for i in idx.get("/".join(parts[:k]), ()):
                    hit.add(i)
            for i, g in globs:
                if fnmatch.fnmatchcase(name, g):
                    hit.add(i)
        for i in hit:
            out[i].append(r)
    return out


def compute(root, now=None):
    cfg = load_config(root)
    default = default_branch(root, cfg.get("default_branch"))
    prefixes = cfg.get("ai_branch_prefixes") or DEFAULT_AI_PREFIXES
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    branches = _branches(root, default)
    notes = _verification_notes(root)
    verif = _verifications(root, notes)
    claims = live_claims(root, now)
    reports = _read_json(os.path.join(_cache(root), "reports.json"), [])
    has_default = bool(git(root, "rev-parse", "--verify", "-q", default, check=False).strip())
    merged_rows = _log_files(root, default) if has_default else []
    reach = _verify_reach(root, default, verif) if has_default and verif else {}
    from . import metrics
    ci_fails = {k for k, v in metrics.ci_status(root).items() if v == "fail"}
    branch_rows = [(b, _log_files(root, "%s..%s" % (default, b))) for b in branches] if has_default else []

    rule = donerule.get(cfg)
    checks = donerule.Checks(root) if rule == "merged+ci" else None
    fcfg = [f for f in cfg.get("features") or [] if isinstance(f, dict) and "id" in f]
    fpaths = [[f["paths"]] if isinstance(f.get("paths"), str) else list(f.get("paths") or []) for f in fcfg]
    merged_by = _bucket(merged_rows, fpaths)
    branch_by = [(b, _bucket(rows, fpaths)) for b, rows in branch_rows]
    features = []
    for fi, f in enumerate(fcfg):
        paths = fpaths[fi]
        why, status, evidence = [], "unknown", {"merged_commits": 0, "verified_commit": None, "verified_note": "",
                                                "unmerged_branches": [], "commits": []}
        if not paths:
            why.append("no paths declared, so no git evidence can be found")
        elif has_default:
            log = merged_by[fi][:defaults.get("max_feature_log")]
            evidence["merged_commits"] = len(log)
            for r in log[:5]:
                kind, tools, _ = classify(r[3], [], prefixes)
                evidence["commits"].append({"short": r[0][:8], "subject": r[2], "kind": kind, "tools": tools,
                                            "time": r[1]})
            # verified = a pass record on a commit at or after the latest merged change to these paths
            ok = None
            if log:
                anyv, fv = reach.get(log[0][0], (None, {}))
                ok = anyv or fv.get(f["id"])
            evidence["latest_time"] = log[0][1] if log else None
            evidence["ci_fail"] = any(r[0] in ci_fails for r in log[:20])
            for b, buckets in branch_by:
                mine = buckets[fi]
                if mine:
                    evidence["unmerged_branches"].append({"branch": b, "commits": len(mine), "last": mine[0][1]})
            if ok:
                status = "done"
                evidence["verified_commit"] = ok[:8]
                evidence["verified_note"] = notes.get(ok, "")[:300]
                why.append("merged into %s and verification record on %s" % (default, ok[:8]))
            elif rule != "strict" and log and not ci_fails & {r[0] for r in log[:20]} and not any(
                    b["last"] > log[0][1] for b in evidence["unmerged_branches"]):
                verdict, src = ("pass", "") if rule == "merged" else checks.result(log[0][0])
                if verdict == "pass":
                    status = "done"
                    evidence["verified_commit"] = log[0][0][:8]
                    evidence["verified_note"] = "git-only rule %s%s" % (rule, (": checks passed (%s)" % src) if src else "")
                    why.append("merged into %s; done by the %s rule%s" % (default, rule, (" (checks passed: %s)" % src) if src else ""))
                else:
                    status = "in-progress" if evidence["unmerged_branches"] else "unknown"
                    why.append("merged into %s but checks %s (%s)" % (default, "failed" if verdict == "fail" else "not known", src))
            elif evidence["unmerged_branches"]:
                status = "in-progress"
                why.append("unmerged commits on: " + ", ".join(
                    b["branch"] for b in evidence["unmerged_branches"]))
                if log:
                    why.append("%d merged commit(s) have no verification record" % len(log))
            elif log:
                why.append("%d commit(s) merged into %s but no verification record" % (len(log), default))
            else:
                why.append("no commits touch these paths")
        else:
            why.append("default branch %r not found" % default)
        c = claims.get(f["id"])
        rep = [r for r in reports if r.get("feature") == f["id"]]
        features.append({
            "id": f["id"], "name": f.get("name") or f["id"], "paths": paths,
            "area": str(f.get("area") or "") or defaults.get("default_area"),
            "target": f["target"] if isinstance(f.get("target"), int) else None, "weight": _num(f.get("weight")),
            "accept": f.get("accept") or "", "status": status, "why": why, "evidence": evidence,
            "claim": c, "reported_done": rep[-1] if rep else None,
            "note": ("reported done by a session; not confirmed by git evidence" if rep and status != "done" else ""),
        })
    if checks:
        checks.save()
    lay = resolve_areas(cfg, features)
    return {"repo": root, "done_rule": rule, "name": cfg.get("name") or os.path.basename(root), "default_branch": default,
            "features": features, "commits": commits(root, default, prefixes), "generated": time.time(),
            "counts": _counts(features, lay), "areas": lay["areas"], "size_by": lay["size_by"],
            "work_unit": lay["work_unit"]}


def _num(v):
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def _area_list(cfg, features):
    """Flat declared areas + areas only features name -> [{name, parent, weight, planned}] in order."""
    out, seen = [], set()
    for a in cfg.get("areas") or []:
        if isinstance(a, dict) and a.get("name") and str(a["name"]) not in seen:
            seen.add(str(a["name"]))
            out.append({"name": str(a["name"]), "parent": str(a["parent"]) if a.get("parent") else "",
                        "weight": _num(a.get("weight")), "planned": a.get("planned") if isinstance(a.get("planned"), int) else 0})
    for f in features:
        if f["area"] not in seen:
            seen.add(f["area"])
            out.append({"name": f["area"], "parent": "", "weight": None, "planned": 0})
    names = {a["name"] for a in out}
    for a in out:  # an unknown parent, or a cycle, makes the area top-level
        p, hops = a["parent"], 0
        while p and hops < len(out):
            if p == a["name"] or p not in names:
                a["parent"] = ""
                break
            p = next(x["parent"] for x in out if x["name"] == p)
            hops += 1
        else:
            if p:
                a["parent"] = ""
    return out


def resolve_areas(cfg, features):
    """Nested areas from dropviz.yaml (`areas:` with `parent:`). One word for every level: an area holds features and
    other areas. -> {"areas": [node], size_by, work_unit}; node = {name, weight, planned, placeholders, features:[ids],
    children:[node]}. Placeholders = planned - features inside (never negative); they are not features."""
    flat = _area_list(cfg, features)
    mine = {}
    for f in features:
        mine.setdefault(f["area"], []).append(f["id"])
    nodes = {a["name"]: dict(a, features=mine.get(a["name"], []), children=[], placeholders=0) for a in flat}
    roots = []
    for a in flat:
        n = nodes[a["name"]]
        (nodes[a["parent"]]["children"] if a["parent"] else roots).append(n)

    def count(n):
        return len(n["features"]) + sum(count(c) for c in n["children"])
    for n in nodes.values():
        n["placeholders"] = max(0, n["planned"] - count(n))
    size_by = cfg.get("size_by") if cfg.get("size_by") in defaults.get("size_rules") else defaults.get("default_size_rule")
    wu = cfg.get("work_unit") if cfg.get("work_unit") in defaults.get("work_units") else defaults.get("default_work_unit")
    return {"areas": roots, "size_by": size_by, "work_unit": wu}


def _walk(nodes):
    for n in nodes:
        yield n
        yield from _walk(n["children"])


def area_tree_out(nodes):
    return [{"name": n["name"], "areas": area_tree_out(n["children"])} for n in nodes]


def save_settings(root, s):
    """Apply a settings change to dropviz.yaml, keeping a copy of the previous file in .dropviz/backups/.
    s may hold: size_by, work_unit, areas (a nested tree [{name, was?, weight?, planned?, areas:[...]}]),
    feature_areas {feature id: area}, feature_weights {id: weight}. Features' paths/accept/target stay.
    -> summary dict. Raises ValueError with a plain message."""
    cfg = load_config(root)
    path = os.path.join(root, "dropviz.yaml")
    if not cfg:
        cfg = {"name": os.path.basename(root), "default_branch": default_branch(root),
               "ai_branch_prefixes": defaults.get("ai_branch_prefixes"), "features": draft_features(root)}
    if "size_by" in s:
        if s["size_by"] not in defaults.get("size_rules"):
            raise ValueError(_err("size_by", options=", ".join(defaults.get("size_rules"))))
        cfg["size_by"] = s["size_by"]
    if "work_unit" in s:
        if s["work_unit"] not in defaults.get("work_units"):
            raise ValueError(_err("work_unit", options=", ".join(defaults.get("work_units"))))
        cfg["work_unit"] = s["work_unit"]
    feats = [f for f in cfg.get("features") or [] if isinstance(f, dict) and "id" in f]
    fid = {f["id"]: f for f in feats}
    renames = {}
    if "areas" in s:
        flat, seen = [], set()

        def walk(nodes, parent, depth):
            if not isinstance(nodes, list):
                raise ValueError(_err("areas_list"))
            if depth > 8:
                raise ValueError(_err("too_deep"))
            for n in nodes:
                name = str((n or {}).get("name") or "").strip()
                if not name or len(name) > 60:
                    raise ValueError(_err("area_name"))
                if name.lower() in seen:
                    raise ValueError(_err("area_dup", name=name))
                seen.add(name.lower())
                item = {"name": name}
                if parent:
                    item["parent"] = parent
                w = _num(n.get("weight"))
                if w:
                    item["weight"] = w
                pl = n.get("planned")
                if pl not in (None, "", 0):
                    if not isinstance(pl, int) or pl < 0 or pl > 5000:
                        raise ValueError(_err("planned"))
                    item["planned"] = pl
                if n.get("was") and n["was"] != name:
                    renames[str(n["was"])] = name
                flat.append(item)
                walk(n.get("areas") or [], name, depth + 1)
        walk(s["areas"], "", 0)
        cfg["areas"] = flat
        for f in feats:
            if f.get("area") in renames:
                f["area"] = renames[f["area"]]
    if "feature_areas" in s:
        names = {a["name"] for a in cfg.get("areas") or []}
        for k, v in (s["feature_areas"] or {}).items():
            if k not in fid:
                raise ValueError(_err("unknown_feature", id=k))
            if v not in names:
                raise ValueError(_err("unknown_area", name=v, known=", ".join(sorted(names))))
            fid[k]["area"] = v
    if "feature_weights" in s:
        for k, w in (s["feature_weights"] or {}).items():
            if k not in fid:
                raise ValueError(_err("unknown_feature", id=k))
            if w in (None, "", 0):
                fid[k].pop("weight", None)
            elif _num(w):
                fid[k]["weight"] = _num(w)
            else:
                raise ValueError(_err("weight"))
    backup = None
    if os.path.isfile(path):
        bdir = os.path.join(_cache(root), "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup, n = os.path.join(bdir, "dropviz.yaml." + stamp), 1
        while os.path.exists(backup):
            n += 1
            backup = os.path.join(bdir, "dropviz.yaml.%s-%d" % (stamp, n))
        shutil.copyfile(path, backup)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(miniyaml.dumps_config(cfg))
    os.replace(tmp, path)
    lay = resolve_areas(cfg, [dict(f, area=str(f.get("area") or defaults.get("default_area"))) for f in feats])
    return {"saved": path, "backup": backup, "size_by": lay["size_by"], "work_unit": lay["work_unit"],
            "areas": area_tree_out(lay["areas"])}


def _counts(features, lay=None):
    c = {"done": 0, "in-progress": 0, "unknown": 0}
    for f in features:
        c[f["status"]] += 1
    c["total"] = len(features)
    # placeholders for planned-but-not-declared features are counted apart and are never "done"
    c["planned"] = sum(n["placeholders"] for n in _walk((lay or {}).get("areas", [])))
    return c


def commits(root, default, prefixes, limit=200):
    sep, rs = "\x1f", "\x1e"
    out = git(root, "log", "--branches", "--remotes", "--tags", "-n", str(limit),
              "--format=%H" + sep + "%an" + sep + "%aI" + sep + "%s" + sep + "%P" + sep + "%B" + rs, check=False)
    on_default = set(git(root, "rev-list", default, check=False).split()) if default else set()
    # which non-default branch tips contain unmerged commits
    owner = {}
    for b in _branches(root, default):
        for c in git(root, "rev-list", "%s..%s" % (default, b), check=False).split():
            owner.setdefault(c, []).append(b)
    rows = []
    for rec in out.split(rs):
        rec = rec.strip("\n")
        if not rec:
            continue
        h, an, ad, subj, parents, body = (rec.split(sep) + [""] * 6)[:6]
        kind, tools, why = classify(body, owner.get(h, []), prefixes)
        rows.append({"hash": h, "short": h[:8], "author": an, "date": ad, "subject": subj,
                     "merge": len(parents.split()) > 1, "kind": kind, "tools": tools, "why": why,
                     "merged": h in on_default, "branches": owner.get(h, [])})
    return rows

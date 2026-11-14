"""Treemap layout and painting for the map page. Pure functions: state in, rectangles out.

Hierarchy: areas (nested to any depth) holding feature tiles. The size rule (`size_by`) and the view (progress, parts, now,
priority, speed, risk, attention) change sizes and colours only. The status of a feature always comes from git
evidence (core.compute); a tile also carries a stripe in its status colour in every view, and the "done" colour
is used for nothing else."""
import time

from . import defaults, i18n

D = defaults.data()
VIEWS = tuple(D["views"])
STATUS_FILL = D["status_colors"]
IDLE = D["idle_color"]
PALETTE = tuple(D["palette"])
VC = D["view_colors"]
L = D["layout"]
DAY = 24 * 3600


def th(name, cfg=None):
    return defaults.get(name, cfg)


def t(lang, key):
    return i18n.t(lang, "map." + key)


def _mix(a, b, k):
    k = max(0.0, min(1.0, k))
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * k) for x, y in zip(pa, pb))


# ---------- per-feature facts ----------
def facts(state, ctx):
    """{feature id: dict} derived only from evidence in `state` and local counters in `ctx`."""
    now = ctx.get("now") or time.time()
    cfg = ctx.get("config")
    stale_b, idle_b, recent_d = th("stale_branch_days", cfg) * DAY, th("idle_branch_days", cfg) * DAY, th("recent_days", cfg) * DAY
    stale_c = th("stale_claim_minutes", cfg) * 60
    aw, ow, wk = th("attention_ask_weight", cfg), th("attention_open_weight", cfg), th("eta_window_weeks", cfg)
    att = ctx.get("attention") or {}
    eta = {e["feature"]: e for e in ((ctx.get("metrics") or {}).get("eta") or [])}
    out = {}
    weeks = []
    if ctx.get("metrics") is not None:
        from . import metrics
        weeks = [metrics._week(now - k * metrics.WEEK) for k in range(wk)]
    for f in state["features"]:
        ev = f["evidence"]
        ub = ev.get("unmerged_branches") or []
        commits = ev.get("merged_commits", 0) + sum(b["commits"] for b in ub)
        a = att.get(f["id"]) or {}
        score = commits + aw * a.get("mcp", 0) + ow * a.get("opens", 0)
        reasons, level = [], 0
        if ev.get("ci_fail"):
            reasons.append("ci-fail")
            level = 3
        if ub and all(now - b.get("last", now) > stale_b for b in ub):
            reasons.append("branch-idle")
            level = max(level, 3)
        if f["status"] != "done" and ev.get("merged_commits", 0) > 0:
            reasons.append("unverified")
            level = max(level, 2)
        c = f.get("claim")
        if c and now - c.get("last", now) > stale_c:
            reasons.append("claim-idle")
            level = max(level, 1)
        if ub and not c and any(now - b.get("last", now) > idle_b for b in ub) and level < 1:
            reasons.append("branch-idle-3d")
            level = 1
        recent = ev.get("latest_time") and now - ev["latest_time"] <= recent_d
        if c:
            act = 3
        elif ub:
            act = 2
        elif recent:
            act = 1
        else:
            act = 0
        w = eta.get(f["id"], {}).get("weekly") or {}
        out[f["id"]] = {"score": score, "risk": level, "risk_why": reasons, "act": act, "commits": commits,
                        "speed": sum(w.get(k, 0) for k in weeks) if ctx.get("metrics") is not None else None,
                        "weight": f.get("weight") or 1.0, "recent": bool(recent)}
    return out


def _leaf_size(f, rule, fx, ctx):
    if rule == "weight":
        return f.get("weight") or 1.0
    if rule == "work":
        if ctx.get("work_unit") == "tokens" and ctx.get("tokens"):
            vals = [v for v in ctx["tokens"].values() if v > 0]
            mean = (sum(vals) / len(vals)) if vals else 1.0
            return max(0.25, ctx["tokens"].get(f["id"], 0) / mean)
        return max(0.25, float(fx["commits"]))
    if rule == "attention":
        return max(0.5, float(fx["score"]))
    return 1.0


def _placeholder_size(rule):
    return {"work": 0.25, "attention": 0.5}.get(rule, 1.0)


def view_rule(view, size_by):
    return {"progress": size_by, "priority": "weight", "attention": "attention"}.get(view, "features")


# ---------- geometry ----------
def squarify(items, x, y, w, h):
    """items [(size, payload)] -> [(payload, x, y, w, h)]. Larger first; equal sizes keep their order."""
    items = [(max(s, 1e-9), p) for s, p in items]
    if not items or w <= 0 or h <= 0:
        return []
    total = sum(s for s, _ in items)
    scale = w * h / total
    rest = [(s * scale, p) for s, p in items]
    out = []

    def worst(row, side):
        s = sum(a for a, _ in row)
        return max(max(side * side * a / (s * s), s * s / (side * side * a)) for a, _ in row)

    while rest:
        side = min(w, h)
        row = [rest[0]]
        i = 1
        while i < len(rest) and worst(row + [rest[i]], side) <= worst(row, side):
            row.append(rest[i])
            i += 1
        rest = rest[i:]
        s = sum(a for a, _ in row)
        if w >= h:  # lay the row along the left edge
            rw = s / h
            yy = y
            for a, p in row:
                rh = a / rw
                out.append((p, x, yy, rw, rh))
                yy += rh
            x, w = x + rw, w - rw
        else:
            rh = s / w
            xx = x
            for a, p in row:
                rw = a / rh
                out.append((p, xx, y, rw, rh))
                xx += rw
            y, h = y + rh, h - rh
    return out


def slice_dir(items, x, y, w, h):
    """Side by side along the longer side, in the given order (top-level areas keep the user's order)."""
    total = sum(max(s, 1e-9) for s, _ in items)
    out, pos = [], 0.0
    for s, p in items:
        k = max(s, 1e-9) / total
        if w >= h:
            out.append((p, x + pos * w, y, k * w, h))
        else:
            out.append((p, x, y + pos * h, w, k * h))
        pos += k
    return out


def fit(text, width, charw=None):
    charw = charw or th("label_char_width")
    n = int((width - 6) / charw)
    if n < 3 or not text:
        return ""
    return text if len(text) <= n else text[:max(1, n - 1)] + "…"


def wrap2(text, width, height):
    """Up to 2 lines of a tile label that fit; [] when the tile is too small."""
    n = int((width - 6) / th("label_char_width"))
    if n < 4 or height < L["tile_min_h"]:
        return []
    if len(text) <= n or height < L["tile_two_line_h"]:
        return [fit(text, width)]
    words, l1 = text.split(), ""
    for i, wd in enumerate(words):
        if len((l1 + " " + wd).strip()) > n:
            break
        l1 = (l1 + " " + wd).strip()
    else:
        return [l1]
    if not l1:
        return [fit(text, width)]
    return [l1, fit(" ".join(words[len(l1.split()):]), width)]


# ---------- the tree ----------
def build(state, rule, ctx, fx, depth=0, nodes=None, parent=None):
    """Area tree with sizes. A node: {name, path, size, leaves [(kind, obj, size)], children [node]}."""
    feats = {f["id"]: f for f in state["features"]}
    out = []
    for n, a in enumerate(nodes if nodes is not None else state["areas"]):
        leaves = [("f", feats[fid], _leaf_size(feats[fid], rule, fx[fid], ctx)) for fid in a["features"]]
        leaves += [("p", {"area": a["name"], "i": i}, _placeholder_size(rule)) for i in range(a["placeholders"])]
        kids = build(state, rule, ctx, fx, depth + 1, a["children"], a)
        size = sum(x[2] for x in leaves) + sum(k["size"] for k in kids)
        out.append({"name": a["name"], "depth": depth, "leaves": leaves, "children": kids, "size": size or 1.0,
                    "explicit": a.get("weight") if rule == "weight" else None})
    # a weight you set means "this many times an average sibling"; areas without one keep the sum of what they hold
    plain = [x["size"] for x in out if not x["explicit"]]
    unit = (sum(plain) / len(plain)) if plain else 1.0
    for x in out:
        if x["explicit"]:
            x["size"] = x["explicit"] * unit
    return out


def all_leaves(node):
    out = list(node["leaves"])
    for k in node["children"]:
        out += all_leaves(k)
    return out


def find_area(state, name, nodes=None, trail=()):
    for a in nodes if nodes is not None else state["areas"]:
        if a["name"] == name:
            return a, trail + (a["name"],)
        r = find_area(state, name, a["children"], trail + (a["name"],))
        if r:
            return r
    return None


STATUS_RANK = {"done": 0, "in-progress": 1, "unknown": 2}


def paint(view, f, fx, ctx, lang, area_idx, maxw, maxscore, maxspeed):
    """-> (fill, tip). The fill never uses the done colour outside the progress view's done tiles."""
    if view == "progress":
        fill = STATUS_FILL[f["status"]]
        return fill, t(lang, f["status"])
    if view == "parts":
        return PALETTE[area_idx % len(PALETTE)], ""
    if view == "now":
        a = fx["act"]
        fill = {3: VC["now_claim"], 2: VC["now_open"], 1: VC["now_recent"], 0: IDLE}[a]
        return fill, t(lang, {3: "now_claim", 2: "now_open", 1: "now_recent", 0: "now_old"}[a])
    if view == "priority":
        w = fx["weight"]
        if maxw <= 1.0 and w == 1.0:
            return IDLE, t(lang, "prio_none")
        return _mix(VC["priority_low"], VC["priority_high"], w / maxw), "%s %s" % (t(lang, "weight"), ("%g" % w))
    if view == "speed":
        if fx["speed"] is None:
            return IDLE, "?"
        return (_mix(IDLE, VC["speed"], fx["speed"] / maxspeed) if fx["speed"] else IDLE,
                "%d %s" % (fx["speed"], t(lang, "speed_l")))
    if view == "risk":
        lv = fx["risk"]
        fill = {3: VC["risk_red"], 2: VC["risk_orange"], 1: VC["risk_yellow"], 0: IDLE}[lv]
        return fill, ", ".join(fx["risk_why"]) or t(lang, "risk_none")
    if view == "attention":
        s = fx["score"]
        return (_mix(IDLE, VC["attention"], s / maxscore) if s else IDLE), "%s %d" % (t(lang, "score"), s)
    return IDLE, ""


def legend(view, lang, size_by):
    P = lambda *a: [{"fill": f, "label": t(lang, k)} for f, k in a]
    S = STATUS_FILL
    if view == "progress":
        items = P((S["done"], "done"), (S["in-progress"], "in-progress"), (S["unknown"], "unknown"), (S["planned"], "planned"))
    elif view == "parts":
        items = P((PALETTE[0], "parts_l"))
    elif view == "now":
        items = P((VC["now_claim"], "now_claim"), (VC["now_open"], "now_open"), (VC["now_recent"], "now_recent"), (IDLE, "now_old"))
    elif view == "priority":
        items = P((VC["priority_high"], "prio"), (IDLE, "prio_none"))
    elif view == "speed":
        items = P((VC["speed"], "speed_l"), (IDLE, "speed_none"))
    elif view == "risk":
        items = P((VC["risk_red"], "risk_red"), (VC["risk_orange"], "risk_orange"), (VC["risk_yellow"], "risk_yellow"), (IDLE, "risk_none"))
    else:
        items = P((VC["attention"], "att"), (IDLE, "att_none"))
    items.append({"fill": "", "label": t(lang, "size_" + size_by if view == "progress" else "size_view_" + view)})
    items += P(("", "claimed"), ("", "unverified"))
    if view != "progress":
        items += P(("", "stripe"))
    return items


def figure(view, lang, leaves):
    """Header figure of a region from its leaves [(kind, f, fx)]."""
    n = len(leaves)
    real = [(f, x) for k, f, x in leaves if k == "f"]
    if view == "progress":
        return "%d%%" % round(100.0 * sum(1 for f, _ in real if f["status"] == "done") / n) if n else ""
    if view == "parts":
        return str(n)
    if view == "now":
        return "%d %s" % (sum(1 for _, x in real if x["act"] >= 2), t(lang, "active"))
    if view == "priority":
        return "%g" % round(sum(x["weight"] for _, x in real), 1)
    if view == "speed":
        v = [x["speed"] for _, x in real]
        return "?" if any(i is None for i in v) else "%.1f%s" % (sum(v) / th("eta_window_weeks"), t(lang, "wk"))
    if view == "risk":
        return "%d %s" % (sum(1 for _, x in real if x["risk"] >= 2), t(lang, "n_risk"))
    return "%d" % sum(x["score"] for _, x in real)


def layout(state, w, h, view="progress", focus="", ctx=None, lang="en", size_by=None, model=""):
    """Rectangles for the map. focus '' = all, else the name of one area (any depth) fills the map.
    -> {"w","h","view","size_by","focus","crumbs","nodes":[...],"legend":[...],"stats":{...}}"""
    ctx = dict(ctx or {})
    ctx.setdefault("now", time.time())
    if view not in VIEWS:
        view = "progress"
    size_by = size_by or state.get("size_by") or "features"
    rule = view_rule(view, size_by)
    if ctx.get("work_unit") is None:
        ctx["work_unit"] = state.get("work_unit", "commits")
    fx = facts(state, ctx)
    maxw = max([x["weight"] for x in fx.values()] or [1.0])
    maxscore = max([x["score"] for x in fx.values()] or [1]) or 1
    maxspeed = max([x["speed"] or 0 for x in fx.values()] or [1]) or 1
    tree = build(state, rule, ctx, fx)
    mdl = ctx.get("models") or {}
    live = ctx.get("live_by_feature") or {}
    nodes, palette_of = [], {}
    for i, r in enumerate(tree):
        palette_of[r["name"]] = i

    def lv(a):
        return [(k, f, fx.get(f["id"]) if k == "f" else None) for k, f, _ in all_leaves(a)]

    def place_leaves(root_i, leaves, x, y, w, h):
        order = sorted(range(len(leaves)), key=lambda i: (
            -leaves[i][2], STATUS_RANK.get(leaves[i][1].get("status"), 3) if leaves[i][0] == "f" else 3, i))
        for (k, f, _), lx, ly, lw, lh in squarify([(leaves[i][2], leaves[i]) for i in order], x, y, w, h):
            if k == "p":
                nodes.append({"key": "p:%s/%d" % (f["area"], f["i"]), "kind": "planned", "x": lx, "y": ly, "w": lw, "h": lh,
                              "status": "planned", "fill": STATUS_FILL["planned"], "stripe": "", "lines": [],
                              "label": "", "tip": t(lang, "planned")})
                continue
            x_ = fx[f["id"]]
            fill, tip = paint(view, f, x_, ctx, lang, root_i, maxw, maxscore, maxspeed)
            n = {"key": "f:" + f["id"], "kind": "feature", "id": f["id"], "x": lx, "y": ly, "w": lw, "h": lh,
                 "status": f["status"], "fill": fill, "stripe": STATUS_FILL[f["status"]], "tip": tip,
                 "lines": wrap2(f["name"], lw, lh), "claimed": bool(f.get("claim")),
                 "unverified": f["status"] != "done" and f["evidence"].get("merged_commits", 0) > 0}
            if model:
                n["model_hit"] = model in (mdl.get(f["id"]) or {})
            if live.get(f["id"]):
                n["live"] = live[f["id"]]
            nodes.append(n)

    def place_area(a, x, y, w, h, root_i, top):
        hdr = (L["hdr_top"] if top else L["hdr_1"] if a["depth"] == 1 else L["hdr_n"]) if h >= (L["min_h_top"] if top else L["min_h"]) and w >= L["min_w"] else 0
        nodes.append({"key": "a:" + a["name"], "kind": "area", "depth": a["depth"], "name": a["name"], "x": x, "y": y, "w": w,
                      "h": h, "hdr": hdr, "fill": L["hdr_fill_top"] if top else _mix(L["hdr_fill"], PALETTE[root_i % len(PALETTE)], L["tint"] / (1 + L["tint_decay"] * (a["depth"] - 1))),
                      "label": fit("%s %s" % (a["name"], figure(view, lang, lv(a))), w, L["top_char_width"] if top else None) if hdr else ""})
        pad = L["pad_top"] if top else L["pad"]
        x0, y0, w0, h0 = x + pad, y + hdr + (1 if hdr else pad), w - 2 * pad, h - hdr - (pad + 1 if hdr else 2 * pad)
        items = [(k["size"], ("a", k)) for k in a["children"]]
        own = sum(s_ for _, _, s_ in a["leaves"])
        if a["leaves"]:
            items.append((own, ("l", a["leaves"])))
        for (kind, obj), cx, cy, cw, ch in squarify(items, x0, y0, w0, h0):
            if kind == "a":
                place_area(obj, cx, cy, cw, ch, root_i, False)
            else:
                place_leaves(root_i, obj, cx + 1, cy + 1, cw - 2, ch - 2)

    W, H = float(w), float(h)
    crumbs = []
    if focus:
        r = find_area(state, focus)
        if r:
            node = next(x for x in _flat(tree) if x["name"] == focus)
            crumbs = [{"name": n} for n in r[1]]
            place_area(node, 0, 0, W, H, palette_of.get(r[1][0], 0), True)
        else:
            focus = ""
    if not focus:
        roots = tree
        if len(roots) == 1 and not roots[0]["leaves"]:
            roots = roots[0]["children"]  # one wrapping area adds nothing
        tops = slice_dir([(r["size"], r) for r in roots], 0, 0, W, H)
        for r, rx, ry, rw, rh in tops:
            place_area(r, rx, ry, rw, rh, palette_of.get(r["name"], 0), True)
    for n in nodes:
        for k in ("x", "y", "w", "h"):
            n[k] = round(n[k], 1)
    tiles = [n for n in nodes if n["kind"] in ("feature", "planned")]
    return {"w": w, "h": h, "view": view, "size_by": size_by, "rule": rule, "focus": focus, "crumbs": crumbs, "nodes": nodes,
            "legend": legend(view, lang, size_by),
            "stats": {"features": sum(1 for n in tiles if n["kind"] == "feature"),
                      "planned": sum(1 for n in tiles if n["kind"] == "planned")}}


def _flat(tree):
    for a in tree:
        yield a
        yield from _flat(a["children"])

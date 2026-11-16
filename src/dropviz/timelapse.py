"""Replay of a repo's history: one frame per day, from the first commit to today.

Built locally from git only (commits, merge times, verification notes). A feature in a frame is:
  done         the newest change merged by the end of that day has a verification record dated by then
  in-progress  unmerged work on it was touched in the last `stale_branch_days` days (live work only)
  partly       work on it was merged by then but not verified yet
  unknown      otherwise
  (absent)     no commit on any branch touched its paths yet: the feature is not drawn on that day
The last frame is replaced by the real current statuses, so the replay always ends on today's map. Claims have no
history in git, so earlier frames show no claim rings."""
import datetime
import os

from . import core, defaults

NOTE_REF = core.NOTES_REF


def _day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _day_end(day):
    d = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    return int((d + datetime.timedelta(days=1)).timestamp()) - 1


def _note_times(root):
    """{commit: time the verification note was added} from the history of the notes ref."""
    out = core.git(root, "log", "refs/notes/" + NOTE_REF, "--diff-filter=AM", "--name-only", "--no-renames",
                   "--format=%x01%ct", check=False)
    times = {}
    for rec in out.split("\x01")[1:]:
        lines = rec.split("\n")
        ts = int(lines[0]) if lines[0].isdigit() else 0
        for n in lines[1:]:
            n = n.strip().replace("/", "")
            if n:
                times[n] = min(times.get(n, ts), ts)  # first time this note appeared
    return times


def _merge_times(root, default):
    """{commit: time it reached the default branch} = time of the first-parent unit that contains it."""
    rows = [l.split() for l in core.git(root, "log", "--topo-order", "--parents", "--format=%H %ct %P", default, check=False).splitlines()]
    info = {r[0]: (int(r[1]), r[2:]) for r in rows if len(r) >= 2}
    chain, c = [], rows[0][0] if rows else None
    while c:
        chain.append(c)
        ps = info[c][1]
        c = ps[0] if ps else None
    out = {}
    for u in reversed(chain):  # oldest unit first: it claims everything it can still reach
        t0, stack = info[u][0], [u]
        while stack:
            x = stack.pop()
            if x in out or x not in info:
                continue
            out[x] = t0
            stack.extend(info[x][1])
    return out


def frames(root, state=None, now=None):
    """-> {"days": ["YYYY-MM-DD", ...], "ids": [feature ids], "frames": [{"day", "s": {id: 'd'|'p'|'a'|'u'}}], "counts": [done per day]}"""
    state = state or core.compute(root)
    default = state["default_branch"]
    code = {"done": "d", "in-progress": "p", "partly": "a", "unknown": "u"}
    rows = core._log_files(root, "--branches", "--remotes", "--tags")
    if not rows:
        return {"days": [], "ids": [], "frames": [], "counts": []}
    merge_t = _merge_times(root, default)
    note_t = _note_times(root)
    notes = core._verification_notes(root)
    verif = core._verifications(root, notes)
    # earliest verification time at or after each commit on the default branch (any scope / per feature)
    revs = [l.split() for l in core.git(root, "rev-list", "--topo-order", "--parents", default, check=False).splitlines()]
    kids = {}
    for r in revs:
        for par in r[1:]:
            kids.setdefault(par, []).append(r[0])
    reach = {}
    inf = float("inf")
    for r in revs:
        c = r[0]
        anyt, feat = inf, {}
        if c in verif:
            tn = note_t.get(c, inf)
            if verif[c] is None:
                anyt = tn
            else:
                for f in verif[c]:
                    feat[f] = tn
        for k in kids.get(c, ()):
            ka, kf = reach[k]
            anyt = min(anyt, ka)
            for f, v in kf.items():
                feat[f] = min(feat.get(f, inf), v)
        reach[c] = (anyt, feat)
    first = min(r[1] for r in rows)
    last = int(now if now is not None else max(r[1] for r in rows))
    last = max(last, max(r[1] for r in rows))
    days = []
    d = datetime.datetime.fromtimestamp(first, datetime.timezone.utc).date()
    end = datetime.datetime.fromtimestamp(last, datetime.timezone.utc).date()
    while d <= end:
        days.append(d.isoformat())
        d += datetime.timedelta(days=1)
    per = []
    win = defaults.get("stale_branch_days", core.load_config(root)) * 86400
    buckets = core._bucket(rows, [f["paths"] for f in state["features"]])
    note_days = {_day(v) for v in note_t.values()}
    for f, mine_rows in zip(state["features"], buckets):
        mine = [(r[0], r[1], merge_t.get(r[0])) for r in mine_rows]
        # the status only changes on a day something happened to the feature or a verification note was added
        bp = set(note_days)
        for c, ct, mt in mine:
            bp.add(_day(ct))
            bp.add(_day(ct + win + 86400))  # the day its work stops counting as recent
            if mt is not None:
                bp.add(_day(mt))
        per.append((f, mine, bp))
    out, counts, cur = [], [], {}
    for day in days:
        T = _day_end(day)
        s, done = {}, 0
        for f, mine, bp in per:
            if day not in bp and f["id"] in cur:
                st = cur[f["id"]]
                s[f["id"]] = st
                done += st == "d"
                continue
            merged = [(c, mt) for c, ct, mt in mine if mt is not None and mt <= T]
            pending = any(ct <= T and T - ct <= win and (mt is None or mt > T) for c, ct, mt in mine)
            st = "u"
            if merged:
                latest = max(merged, key=lambda x: x[1])[0]
                anyt, ft = reach.get(latest, (inf, {}))
                if min(anyt, ft.get(f["id"], inf)) <= T:
                    st = "d"
            if merged:
                if st != "d" and state.get("done_rule", "strict") != "strict":
                    newest = max(mt for c, mt in merged)   # git-only rules: merged, and no newer unmerged work
                    if not any(ct <= T and ct > newest and (mt is None or mt > T) for c, ct, mt in mine):
                        st = "d"
                if st == "d" and f.get("missing"):
                    st = "a"                               # the feature itself says something is left
                elif st == "u":
                    st = "a"                               # merged, not verified
            if st != "d" and pending:
                st = "p"
            if st == "u" and not merged:
                st = "x"                      # no commit touches it yet: it does not exist on this day
            cur[f["id"]] = st
            s[f["id"]] = st
            done += st == "d"
        out.append({"day": day, "s": s})
        counts.append(done)
    # the last frame is the real current map
    out[-1]["s"] = {f["id"]: code[f["status"]] for f in state["features"]}
    counts[-1] = sum(1 for f in state["features"] if f["status"] == "done")
    return {"days": days, "ids": [f["id"] for f in state["features"]], "frames": out, "counts": counts}


def state_at(state, frame):
    """A copy of `state` whose statuses are those of one frame (everything else unchanged, claims dropped)."""
    name = {"d": "done", "p": "in-progress", "a": "partly", "u": "unknown"}
    fs = []
    for f in state["features"]:
        code = frame["s"].get(f["id"], "u")
        # a feature with no commit yet keeps its slot (the map keeps its shape) but is drawn empty
        fs.append(dict(f, status=name.get(code, "unknown"), claim=None, absent=(code == "x")))
    return dict(state, features=fs)

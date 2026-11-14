#!/usr/bin/env python3
"""Build a synthetic example repository (the invented app "kestrel", data in examples/demo_project.json).

    python3 examples/make_demo_repo.py /tmp/kestrel [--now EPOCH] [--seed N]

About 180 features in nested areas, ~400 commits over ten weeks, merged AI-tool branches with trailers, a few open
AI branches, verification notes (the newest merges are still unverified), a stale branch, a failed CI record, two
claims, and fake Claude Code / Codex / OpenCode records next to the repo (<dir>-sessions) so tokens and models
show. Same seed and --now give the same repo. Everything here is invented; nobody real appears in it."""
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DAY = 86400
AT = chr(64)
TOOLS = {  # tool -> (trailer name, trailer e-mail, models used in the fake records)
    "claude": ("Claude Opus 4.5", "noreply" + AT + "anthropic.com", ("claude-opus-4-5", "claude-sonnet-4-5")),
    "codex": ("OpenAI Codex", "noreply" + AT + "openai.com", ("gpt-5-codex",)),
    "opencode": ("opencode", "noreply" + AT + "opencode.ai", ("zai/glm-4.6",)),
    "gemini": ("Gemini CLI", "noreply" + AT + "google.com", ()),
}
TOOL_WEIGHTS = (("claude", 50), ("codex", 30), ("opencode", 12), ("gemini", 8))


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def load_project():
    with open(os.path.join(HERE, "demo_project.json"), encoding="utf-8") as f:
        return json.load(f)


def flatten(areas, trail=()):
    """-> (area rows [{name, parent, planned}], feature rows [{id, name, area, root, dir, slug}])"""
    rows, feats = [], []
    for a in areas:
        rows.append({"name": a["name"], "parent": trail[-1] if trail else "", "planned": a.get("planned", 0)})
        for fname in (x.strip() for x in a.get("features", "").split(",") if x.strip()):
            feats.append({"id": slug(a["name"] + "-" + fname), "name": fname, "area": a["name"], "root": (trail or (a["name"],))[0],
                          "dir": "src/" + "/".join(slug(x) for x in trail + (a["name"],)), "slug": slug(fname)})
        r2, f2 = flatten(a.get("areas", []), trail + (a["name"],))
        rows += r2
        feats += f2
    return rows, feats


class Stream:
    """git fast-import stream writer with marks."""

    def __init__(self):
        self.out = []
        self.mark = 0

    def data(self, text):
        b = text.encode("utf-8")
        self.out.append(b"data %d\n" % len(b))
        self.out.append(b + b"\n")

    def commit(self, ref, when, author, msg, files=(), parent=None, merge=None):
        self.mark += 1
        n = self.mark
        name, email = author
        self.out.append(("commit %s\nmark :%d\nauthor %s <%s> %d +0000\ncommitter %s <%s> %d +0000\n" %
                         (ref, n, name, email, when, name, email, when)).encode())
        self.data(msg)
        if parent:
            self.out.append(("from :%d\n" % parent).encode())
        if merge:
            self.out.append(("merge :%d\n" % merge).encode())
        for path, text in files:
            self.out.append(("M 100644 inline %s\n" % path).encode())
            self.data(text)
        return n

    def note(self, when, target, text):
        self.mark += 1
        self.out.append(("commit refs/notes/dropviz-verify\nmark :%d\ncommitter CI <ci%sexample.invalid> %d +0000\n" %
                         (self.mark, AT, when)).encode())
        self.data("verification note")
        self.out.append(("N inline :%d\n" % target).encode())
        self.data(text)

    def bytes(self):
        return b"".join(self.out)


def trailer(msg, tool):
    if not tool:
        return msg
    name, email, _ = TOOLS[tool]
    return msg + "\n\nCo-Authored-By: %s <%s>" % (name, email)


def build(now, seed):
    proj = load_project()
    rng = random.Random(seed)
    area_rows, feats = flatten(proj["areas"])
    people = [(n, n.split()[0].lower() + AT + "example.invalid") for n in proj["people"]]
    mat, start = proj["maturity"], proj["start_days_ago"]
    st = Stream()
    tail_start, cutoff = now - 40 * DAY // 24, now - 44 * DAY // 24  # unverified tail: last 40 h; newest pass 44 h ago
    ci_break = (now - 30 * DAY, now - 28 * DAY)
    story = {}
    for f in feats:
        m, r = mat.get(f["area"], 0.5), rng.random()
        story[f["id"]] = "merged" if r < m else ("open" if r < m + (1 - m) * 0.45 else "none")
    merged = [f for f in feats if story[f["id"]] == "merged"]
    for f in rng.sample([f for f in merged if f["root"] != "Web"], 11):
        story[f["id"]] = "tail"
    events = []
    for f in feats:
        s = story[f["id"]]
        if s in ("merged", "tail"):
            t0 = now - start[f["root"]] * DAY
            t = rng.randint(tail_start, now - 600) if s == "tail" else int(t0 + (cutoff - 3 * DAY - t0) * rng.random() ** 0.8)
            how = rng.choices(("ai", "branch", "direct"), (55, 30, 15))[0]
            tool = rng.choices([k for k, _ in TOOL_WEIGHTS], [w for _, w in TOOL_WEIGHTS])[0] if how == "ai" else None
            events.append((t, "feature", f, {"how": how, "tool": tool, "author": rng.choice(people)}))
    first_t = {e[2]["id"]: e[0] for e in events}
    for f in rng.sample([f for f in merged if story[f["id"]] == "merged"], 46):
        t = first_t[f["id"]] + rng.randint(2, 12) * DAY
        if t < cutoff - DAY and not (ci_break[0] - DAY < t < ci_break[1] + DAY):
            events.append((t, "fix", f, {"author": rng.choice(people), "tool": rng.choice(("claude", "codex", None))}))
    for i in range(28):
        events.append((now - rng.randint(3, 69) * DAY - rng.randint(0, DAY - 1), "chore", None, {"n": i, "author": rng.choice(people)}))
    events.sort(key=lambda e: e[0])

    yaml_lines = ['name: "%s"' % proj["name"], 'default_branch: "main"', 'size_by: "features"',
                  'ai_branch_prefixes: ["codex/", "claude/", "gemini/", "opencode/"]', "areas:"]
    for a in area_rows:
        yaml_lines.append('  - name: "%s"' % a["name"])
        if a["parent"]:
            yaml_lines.append('    parent: "%s"' % a["parent"])
        if a["planned"]:
            yaml_lines.append("    planned: %d" % a["planned"])
    yaml_lines.append("features:")
    for f in feats:
        yaml_lines += ['  - id: "%s"' % f["id"], '    name: "%s"' % f["name"], '    area: "%s"' % f["area"],
                       '    paths: ["%s/%s.py", "tests/%s_test.py"]' % (f["dir"], f["slug"], f["slug"]),
                       '    accept: "%s works as described in the product notes."' % f["name"]]
    main = st.commit("refs/heads/main", now - 71 * DAY, people[0], "Start kestrel",
                     [("README.md", "# kestrel\n\nA fictional photo library used only to try dropviz.\n"),
                      ("dropviz.yaml", "\n".join(yaml_lines) + "\n")])
    transcripts, n_units = [], 1

    for t, kind, f, x in events:
        if kind == "feature":
            impl, test = "%s/%s.py" % (f["dir"], f["slug"]), "tests/%s_test.py" % f["slug"]
            title = "%s: %s" % (f["area"].lower(), f["name"].lower())
            if x["how"] == "direct":
                main = st.commit("refs/heads/main", t, x["author"], title, [(impl, "# %s\n" % f["name"]), (test, "# tests\n")], parent=main)
            else:
                tool = x["tool"]
                branch = (tool + "/" if tool else rng.choice(("feature/", x["author"][0].split()[0].lower() + "/"))) + f["slug"]
                bt, parent, kept = t - rng.randint(1, 8) * 3600, main, None
                for i in range(rng.randint(1, 3)):
                    ct = bt + i * rng.randint(600, 2400)
                    msg = title if i == 0 else rng.choice(("%s: tests", "%s: handle edge case", "%s: tidy")) % title
                    kept = st.commit("refs/heads/_tmp", ct, x["author"], trailer(msg, tool),
                                     [(impl, "# %s (rev %d)\n" % (f["name"], i + 1))] + ([(test, "# tests\n")] if i else []), parent=parent)
                    parent = kept
                    if tool:
                        transcripts.append((ct, branch, tool))
                main = st.commit("refs/heads/main", t, x["author"], "Merge branch '%s'" % branch, parent=main, merge=kept)
        elif kind == "fix":
            main = st.commit("refs/heads/main", t, x["author"], trailer("%s: fix an edge case" % f["area"].lower(), x["tool"]),
                             [("%s/%s.py" % (f["dir"], f["slug"]), "# %s (fixed)\n" % f["name"])], parent=main)
            if x["tool"]:
                transcripts.append((t - 900, "main", x["tool"]))
        else:
            main = st.commit("refs/heads/main", t, x["author"], rng.choice(("docs: update notes", "chore: bump tooling", "ci: tweak workflow")),
                             [("docs/note-%02d.md" % x["n"], "note %d\n" % x["n"])], parent=main)
        n_units += 1
        if t < cutoff and not (ci_break[0] <= t <= ci_break[1]) and rng.random() < 0.55:
            st.note(t + 900, main, "pass\nci run: %d tests, 0 failed" % (300 + n_units))
    # open branches: in-progress work, forked from the current tip
    by_area = {}
    for f in feats:
        if story[f["id"]] == "open":
            by_area.setdefault(f["area"], []).append(f)
    groups = list(by_area.items())
    rng.shuffle(groups)
    packs, cur = [], []
    for g in groups:
        cur.append(g)
        if len(cur) == 2 or sum(len(x[1]) for x in cur) >= 4:
            packs.append(cur)
            cur = []
    if cur:
        packs.append(cur)
    opens = []
    for i, grp in enumerate(packs):
        tool = rng.choices([k for k, _ in TOOL_WEIGHTS], [w for _, w in TOOL_WEIGHTS])[0]
        author, bname, stale, parent = rng.choice(people), "%s/%s-work" % (tool, slug(grp[0][0])), i == len(packs) - 1, main
        for a, fl in grp:
            for f in fl:
                ct = (now - 12 * DAY - rng.randint(0, DAY)) if stale else (now - rng.randint(900, 5 * DAY))
                parent = st.commit("refs/heads/" + bname, ct, author, trailer("%s: %s (work in progress)" % (a.lower(), f["name"].lower()), tool),
                                   [("%s/%s.py" % (f["dir"], f["slug"]), "# %s (draft)\n" % f["name"])], parent=parent)
                transcripts.append((ct, bname, tool))
        opens.append((bname, tool, [f["id"] for _, fl in grp for f in fl], stale))
    return st, opens, transcripts, feats, story, cutoff, people


def write_sessions(sdir, target, transcripts, now, rng):
    """Fake AI-tool records (counts, timestamps, cwd, branch, model). Never any message text."""
    cwd = os.path.realpath(target)
    proj = os.path.join(sdir, "claude", "".join(c if c.isalnum() else "-" for c in cwd))
    os.makedirs(proj, exist_ok=True)
    iso = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))
    lines, cx, oc, cum, n = [], [], [], [0, 0, 0], 0
    for ct, branch, tool in sorted(transcripts):
        n += 1
        for k in range(rng.randint(2, 4)):
            ts = ct - 300 - 400 * k
            tk = (rng.randint(200, 900), rng.randint(20000, 90000), rng.randint(1000, 6000), rng.randint(800, 4000))
            if tool == "claude":
                lines.append(json.dumps({"type": "assistant", "cwd": cwd, "gitBranch": branch, "timestamp": iso(ts), "message": {
                    "id": "msg_%d_%d" % (n, k), "model": TOOLS["claude"][2][(n + k) % 2],
                    "usage": {"input_tokens": tk[0], "cache_read_input_tokens": tk[1], "cache_creation_input_tokens": tk[2], "output_tokens": tk[3]}}}))
            elif tool == "codex":
                cum[0] += tk[1] + tk[0]
                cum[1] += tk[1]
                cum[2] += tk[3]
                cx.append((ts, {"timestamp": iso(ts), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": cum[0], "cached_input_tokens": cum[1], "output_tokens": cum[2]}}}}))
            elif tool == "opencode":
                oc.append((ts, tk))
    with open(os.path.join(proj, "demo-claude.jsonl"), "w") as f:
        f.write("\n".join(lines) + "\n")
    cdir = os.path.join(sdir, "codex", "2000", "01", "01")
    os.makedirs(cdir, exist_ok=True)
    head = [{"timestamp": iso(now - 80 * DAY), "type": "session_meta", "payload": {"cwd": cwd, "timestamp": iso(now - 80 * DAY)}},
            {"timestamp": iso(now - 80 * DAY), "type": "turn_context", "payload": {"model": TOOLS["codex"][2][0]}}]
    with open(os.path.join(cdir, "rollout-demo-codex.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(x) for x in head + [e for _, e in sorted(cx, key=lambda z: z[0])]) + "\n")
    db = os.path.join(sdir, "opencode.db")
    if os.path.exists(db):
        os.unlink(db)
    con = sqlite3.connect(db)
    con.execute("create table message (id text, session_id text, time_created integer, data text)")
    for i, (ts, tk) in enumerate(sorted(oc)):
        data = {"role": "assistant", "providerID": "zai", "modelID": "glm-4.6", "path": {"cwd": cwd}, "time": {"completed": int(ts * 1000)},
                "tokens": {"input": tk[0], "output": tk[3], "reasoning": 0, "cache": {"read": tk[1], "write": tk[2]}}}
        con.execute("insert into message values (?,?,?,?)", ("m%d" % i, "oc-demo", int(ts * 1000), json.dumps(data)))
    con.commit()
    con.close()
    return proj


def main(target, now=None, seed=7):
    target = os.path.abspath(target)
    if os.path.exists(target) and os.listdir(target):
        sys.exit("%s is not empty" % target)
    os.makedirs(target, exist_ok=True)
    now = int(now if now is not None else time.time())
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def git(*a, inp=None, e=None):
        p = subprocess.run(["git", "-C", target] + list(a), env=e or env, input=inp, capture_output=True)
        if p.returncode:
            sys.exit(p.stderr.decode())
        return p.stdout.decode().strip()

    git("init", "-q", "-b", "main")
    st, opens, transcripts, feats, story, cutoff, people = build(now, seed)
    rng = random.Random(seed + 1)
    git("fast-import", "--quiet", inp=st.bytes())
    git("checkout", "-q", "-f", "main")
    git("update-ref", "-d", "refs/heads/_tmp")
    # nightly CI pass on the newest main commit of that moment; none during a broken stretch or in the unverified tail
    order = [l.split() for l in git("log", "--first-parent", "--format=%H %ct", "main").splitlines()][::-1]
    broken = (now - 30 * DAY, now - 28 * DAY)
    day, passes = int(order[0][1]) // DAY * DAY + DAY, []
    while day - 1800 <= cutoff:
        t = day - 1800
        shas = [h for h, ct in order if int(ct) <= t]
        if shas and not (broken[0] <= t <= broken[1]):
            passes.append((t, shas[-1]))
        day += DAY
    passes.append((cutoff, [h for h, ct in order if int(ct) <= cutoff][-1]))
    for t, sha in passes:
        e = dict(env, GIT_AUTHOR_NAME="CI", GIT_AUTHOR_EMAIL="ci" + AT + "example.invalid", GIT_COMMITTER_NAME="CI",
                 GIT_COMMITTER_EMAIL="ci" + AT + "example.invalid", GIT_COMMITTER_DATE="%d +0000" % t, GIT_AUTHOR_DATE="%d +0000" % t)
        git("notes", "--ref=dropviz-verify", "append", "-m", "pass\nci run (nightly): all tests passed", sha, e=e)
    sdir = target + "-sessions"
    write_sessions(sdir, target, transcripts, now, rng)
    state = os.path.join(target, ".dropviz")
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, ".gitignore"), "w") as f:
        f.write("*\n")
    live = [(b, ids) for b, tool, ids, stale in opens if not stale]
    claims = {}
    for i, (b, ids) in enumerate(live[:2]):
        claims[ids[0]] = {"session": "mcp-demo-%d" % (i + 1), "since": now - 3600, "last": now - (300 if i == 0 else 3000), "note": ""}
    with open(os.path.join(state, "claims.json"), "w") as f:
        json.dump(claims, f)
    tails = [f for f in feats if story[f["id"]] == "tail"]
    if tails:
        sha = git("log", "main", "-n", "1", "--format=%H", "--", "%s/%s.py" % (tails[0]["dir"], tails[0]["slug"]))
        with open(os.path.join(state, "ci-status.json"), "w") as f:
            json.dump({sha: "fail"}, f)
    att = {fid: {"mcp": rng.randint(0, 5), "opens": rng.randint(0, 6)} for fid in rng.sample([f["id"] for f in feats], 25)}
    with open(os.path.join(state, "attention.json"), "w") as f:
        json.dump(att, f)
    # live sessions: a Codex session in a real worktree, a fresh Claude one, and one that went quiet
    cwd = os.path.realpath(target)
    iso = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))
    names = [b for b, _ in live]
    if names:
        wt = target + "-worktrees/" + names[0].replace("/", "-")
        git("worktree", "add", "-q", wt, names[0])
        rows = [{"timestamp": iso(now - 1500), "type": "session_meta", "payload": {"cwd": os.path.realpath(wt), "timestamp": iso(now - 1500)}},
                {"timestamp": iso(now - 1400), "type": "turn_context", "payload": {"model": TOOLS["codex"][2][0]}},
                {"timestamp": iso(now - 200), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 5}}}}]
        with open(os.path.join(sdir, "codex", "2000", "01", "01", "rollout-live-codex.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in rows) + "\n")
        proj = os.path.join(sdir, "claude", "".join(c if c.isalnum() else "-" for c in cwd))
        for br, ago, model in ((names[-1], 120, TOOLS["claude"][2][0]), (names[min(1, len(names) - 1)], 50 * 60, TOOLS["claude"][2][1])):
            lines = []
            for k, off in enumerate((1800, 0)):
                lines.append(json.dumps({"type": "assistant", "cwd": cwd, "gitBranch": br, "timestamp": iso(now - ago - off),
                                         "message": {"id": "live_%d_%d" % (ago, k), "model": model, "usage": {"input_tokens": 1}}}))
            with open(os.path.join(proj, "live-claude-%d.jsonl" % ago), "w") as f:
                f.write("\n".join(lines) + "\n")
    print("demo repo: %s  (%d features, %s commits, %d open branches)\nfake AI records: %s" % (
        target, len(feats), git("rev-list", "--count", "--branches", "main"), len(opens), sdir))
    print("try:\n  DROPVIZ_CLAUDE_DIR=%s/claude DROPVIZ_CODEX_DIR=%s/codex DROPVIZ_OPENCODE_DB=%s/opencode.db \\\n"
          "    python3 -m dropviz serve %s --open" % (sdir, sdir, sdir, target))
    return target


if __name__ == "__main__":
    args, now, seed = sys.argv[1:], None, 7
    for flag in ("--now", "--seed"):
        if flag in args:
            i = args.index(flag)
            val = int(args[i + 1])
            now, seed = (val, seed) if flag == "--now" else (now, val)
            del args[i:i + 2]
    main(args[0] if args else "kestrel", now, seed)

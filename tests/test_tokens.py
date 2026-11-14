import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import helper
from tests.helper import ENV, sh

from dropviz import cli, core, tokens

T = 1767225600  # fixed base time, well before "now"
FIELDS = tokens.FIELDS


def iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def dated_commit(repo, name, when):
    helper.write(repo, name, name + "\n")
    env = dict(ENV, GIT_AUTHOR_DATE="%d +0000" % when, GIT_COMMITTER_DATE="%d +0000" % when)
    subprocess.run(["git", "-C", repo, "add", "-A"], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", name], env=env, check=True, capture_output=True)
    return sh(repo, "rev-parse", "HEAD")


class Fixture:
    """Fake repo (main + worktree on feat + detached worktree) and fake Claude/Codex/OpenCode records."""

    def __init__(self):
        base = os.path.realpath(tempfile.mkdtemp(prefix="dz-tok-"))
        self.base = base
        self.repo = os.path.join(base, "repo")
        os.makedirs(self.repo)
        sh(self.repo, "init", "-q", "-b", "main")
        self.k1 = dated_commit(self.repo, "k1", T + 100)
        self.k2 = dated_commit(self.repo, "k2", T + 200)
        self.k3 = dated_commit(self.repo, "k3", T + 300)
        self.wa = os.path.join(base, "wt-a")
        self.wb = os.path.join(base, "wt-b")
        sh(self.repo, "worktree", "add", "-q", "-b", "feat", self.wa)
        sh(self.repo, "worktree", "add", "-q", "--detach", self.wb)
        self.f1 = dated_commit(self.wa, "f1", T + 400)
        self.f2 = dated_commit(self.wa, "f2", T + 500)
        os.makedirs(os.path.join(self.wa, "sub"))
        self.claude = os.path.join(base, "claude")
        self.codex = os.path.join(base, "codex")
        self.db = os.path.join(base, "oc.db")
        self.expected = {}  # tool -> field sums of every in-repo record, counted once
        self.env = {"DROPVIZ_CLAUDE_DIR": self.claude, "DROPVIZ_CODEX_DIR": self.codex, "DROPVIZ_OPENCODE_DB": self.db}
        self._claude()
        self._codex()
        self._opencode()

    def want(self, tool, i=0, cr=0, cw=0, o=0):
        e = self.expected.setdefault(tool, dict.fromkeys(FIELDS, 0))
        for k, v in zip(FIELDS, (i, cr, cw, o)):
            e[k] += v

    def _claude(self):
        d = os.path.join(self.claude, re.sub(r"[^A-Za-z0-9]", "-", self.repo))
        os.makedirs(d)

        def line(mid, ts, cwd, branch, usage, tool_cmd=None):
            content = [{"type": "text", "text": "SECRET-TEXT"}]
            if tool_cmd:
                content.append({"type": "tool_use", "name": "Bash", "input": {"command": tool_cmd}})
            return json.dumps({"type": "assistant", "cwd": cwd, "gitBranch": branch, "timestamp": iso(ts),
                               "message": {"id": mid, "model": "m", "content": content, "usage": usage}})

        def u(i, cr, cw, o):
            return {"input_tokens": i, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw, "output_tokens": o}
        rows = [
            line("m1", T + 50, self.repo, "main", u(10, 1, 2, 1)),            # streamed: same id twice, per-field max
            line("m1", T + 51, self.repo, "main", u(10, 1, 2, 3)),
            line("m2", T + 150, self.repo, "main", u(20, 2, 4, 6)),
            line("m4", T + 190, self.repo, "main", u(30, 3, 6, 9), "git -C . commit -m x"),  # exact boundary of k2
            line("m5", T + 195, self.repo, "main", u(40, 4, 8, 12)),          # before k2's time, after its commit call
            line("m3", T + 250, self.repo, "main", u(50, 5, 10, 15)),
            line("m6", T + 450, os.path.join(self.wa, "sub"), "feat", u(60, 6, 12, 18)),
            line("m7", T + 160, os.path.join(self.base, "elsewhere"), "main", u(999, 999, 999, 999)),  # other repo
            line("m8", T + 900, self.repo, "main", u(70, 7, 14, 21)),         # after last commit
            line("m9", T + 120, self.repo, "ghost", u(80, 8, 16, 24)),        # branch that does not exist
        ]
        with open(os.path.join(d, "s1.jsonl"), "w") as f:
            f.write("\n".join(rows) + "\n")
        self.want("claude", 10, 1, 2, 3)
        for i, (a, b, c, o) in enumerate([(20, 2, 4, 6), (30, 3, 6, 9), (40, 4, 8, 12), (50, 5, 10, 15), (60, 6, 12, 18),
                                          (70, 7, 14, 21), (80, 8, 16, 24)]):
            self.want("claude", a, b, c, o)

    def _codex(self):
        d = os.path.join(self.codex, "2026", "01", "01")
        os.makedirs(d)

        def tc(ts, i, cached, o):
            return json.dumps({"timestamp": iso(ts), "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": i, "cached_input_tokens": cached, "output_tokens": o}}}})
        meta = lambda cwd: json.dumps({"timestamp": iso(T), "type": "session_meta", "payload": {"cwd": cwd}})
        with open(os.path.join(d, "rollout-a.jsonl"), "w") as f:
            f.write("\n".join([meta(self.wa), tc(T + 350, 50, 10, 5), tc(T + 420, 100, 40, 10), tc(T + 430, 100, 40, 10),
                               tc(T + 440, 150, 60, 25)]) + "\n")
        self.want("codex", 90, 60, 0, 25)  # last cumulative: input 150-60, cached 60, output 25
        with open(os.path.join(d, "rollout-b.jsonl"), "w") as f:  # detached worktree: branch cannot be known
            f.write("\n".join([meta(self.wb), tc(T + 250, 7, 2, 1)]) + "\n")
        self.want("codex", 5, 2, 0, 1)
        with open(os.path.join(d, "rollout-c.jsonl"), "w") as f:  # another repo
            f.write("\n".join([meta(os.path.join(self.base, "other")), tc(T + 250, 500, 0, 500)]) + "\n")

    def _opencode(self):
        con = sqlite3.connect(self.db)
        con.execute("create table message (id text primary key, session_id text, time_created integer, time_updated integer, data text)")

        def ins(mid, ts, data):
            con.execute("insert into message values (?,?,?,?,?)", (mid, "ses", int(ts * 1000), int(ts * 1000), json.dumps(data)))
        ins("a", T + 120, {"role": "assistant", "path": {"cwd": self.repo}, "tokens": {"input": 7, "output": 2, "reasoning": 1,
                                                                                       "cache": {"read": 4, "write": 5}}})
        ins("b", T + 121, {"role": "user", "path": {"cwd": self.repo}})
        ins("c", T + 122, {"role": "assistant", "path": {"cwd": "/nowhere/else"}, "tokens": {"input": 1000, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}})
        con.commit()
        con.close()
        self.want("opencode", 7, 4, 5, 3)


class Tokens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.res = tokens.tokens(cls.fx.repo, sources={"claude": cls.fx.claude, "codex": cls.fx.codex, "opencode": cls.fx.db})

    def per(self, sha, tool):
        return {k: self.res["commits"].get(sha, {}).get(tool, {}).get(k, 0) for k in FIELDS}

    def test_no_loss_no_double_count(self):
        fx, res = self.fx, self.res
        for tool, exp in fx.expected.items():
            got = dict.fromkeys(FIELDS, 0)
            for per in res["commits"].values():
                for k in FIELDS:
                    got[k] += per.get(tool, {}).get(k, 0)
            for per in res["unattributed"]["by_tool"].get(tool, {}), :
                for k in FIELDS:
                    got[k] += per.get(k, 0)
            self.assertEqual(got, exp, tool)
            self.assertEqual({k: res["total"][tool][k] for k in FIELDS}, exp, tool)
        self.assertEqual(sorted(res["total"]), ["claude", "codex", "opencode"])

    def test_claude_dedup_and_windows(self):
        fx = self.fx
        self.assertEqual(self.per(fx.k1, "claude"), {"input": 10, "cache_read": 1, "cache_write": 2, "output": 3})  # max of duplicates
        self.assertEqual(self.per(fx.k2, "claude"), {"input": 20 + 30, "cache_read": 5, "cache_write": 10, "output": 15})
        # m5 sits after the session's own `git commit` call: it belongs to k3 even though its time precedes k2's
        self.assertEqual(self.per(fx.k3, "claude"), {"input": 40 + 50, "cache_read": 9, "cache_write": 18, "output": 27})
        self.assertEqual(self.per(fx.f2, "claude")["input"], 60)

    def test_unattributed_buckets(self):
        r = self.res["unattributed"]["reasons"]
        self.assertEqual(r["after-last-commit"]["claude"]["input"], 70)
        self.assertEqual(r["unknown-branch"]["claude"]["input"], 80)
        self.assertEqual(r["unknown-branch"]["codex"]["input"], 5)  # detached worktree: ambiguous, kept, not zeroed

    def test_codex_deltas_and_opencode(self):
        fx = self.fx
        self.assertEqual(self.per(fx.f1, "codex"), {"input": 40, "cache_read": 10, "cache_write": 0, "output": 5})
        self.assertEqual(self.per(fx.f2, "codex"), {"input": 50, "cache_read": 50, "cache_write": 0, "output": 20})
        self.assertEqual(self.per(fx.k2, "opencode"), {"input": 7, "cache_read": 4, "cache_write": 5, "output": 3})

    def test_other_repo_ignored(self):
        flat = json.dumps(self.res)
        self.assertNotIn("999", flat.replace("T+", ""))
        for tool in ("claude", "codex", "opencode"):
            self.assertLess(self.res["total"][tool]["input"], 500)

    def test_branch_at_time_from_reflog(self):
        fx = self.fx
        sh(fx.wa, "checkout", "-q", "-b", "other")  # HEAD of wt-a moves now
        rec = lambda ts: {"tool": "codex", "ts": ts, "cwd": fx.wa, "branch": None, "session": "s",
                          "input": 1, "cache_read": 0, "cache_write": 0, "output": 0}
        repo = tokens.Repo(fx.repo)
        rows = tokens.attribute(repo, [rec(T + 450), rec(tokens.time.time() + 1000)], {})
        self.assertEqual([r[2] for r in rows], ["feat", "other"])  # before the checkout: feat; after: other
        self.assertEqual(rows[0][1], fx.f2)

    def test_since_and_filters(self):
        fx = self.fx
        src = {"claude": fx.claude, "codex": fx.codex, "opencode": fx.db}
        r = tokens.tokens(fx.repo, sources=src, since="2999-01-01")
        self.assertEqual(r["total"], {})
        r = tokens.tokens(fx.repo, sources=src, commit=fx.k2[:10])
        self.assertEqual(list(r["commits"]), [fx.k2])
        self.assertEqual(r["unattributed"]["by_tool"], {})
        r = tokens.tokens(fx.repo, sources=src, branch="feat")
        self.assertEqual(sorted(r["commits"]), sorted([fx.f1, fx.f2]))

    def test_feature_sum_and_notes(self):
        fx = self.fx
        feats = [{"id": "w", "paths": ["f1", "f2"]}, {"id": "n", "paths": []}]
        ft = tokens.feature_tokens(fx.repo, self.res, feats)
        self.assertEqual(ft["w"]["codex"]["input"], 90)
        self.assertNotIn("n", ft)
        self.assertEqual(sh(fx.repo, "notes", "--ref=" + tokens.NOTES_REF, "list"), "")  # nothing written unasked
        self.assertEqual(tokens.write_notes(fx.repo, self.res), len(self.res["commits"]))
        self.assertIn("claude input=10 cache_read=1 cache_write=2 output=3", sh(fx.repo, "notes", "--ref=" + tokens.NOTES_REF, "show", fx.k1))
        self.assertNotIn("SECRET-TEXT", json.dumps(self.res))

    def test_cli_json_and_text(self):
        with mock.patch.dict(os.environ, self.fx.env):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["tokens", self.fx.repo, "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue())["total"]["opencode"]["input"], 7)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["tokens", self.fx.repo]), 0)
            self.assertIn("cache_write", out.getvalue())
            self.assertIn("unknown-branch", out.getvalue())


class GoneWorktrees(unittest.TestCase):
    """Short-lived worker worktrees that were deleted after the work was merged."""

    def setUp(self):
        base = self.base = os.path.realpath(tempfile.mkdtemp(prefix="dz-gone-"))
        self.repo = os.path.join(base, "repo")
        os.makedirs(self.repo)
        sh(self.repo, "init", "-q", "-b", "main")
        self.k1 = dated_commit(self.repo, "k1", T + 100)
        self.k2 = dated_commit(self.repo, "k2", T + 200)
        self.gone = os.path.join(base, "zw")  # glob root for workers
        for name, br in (("a", "feat-keep"), ("b", "feat-merged")):
            wt = os.path.join(self.gone, name)
            sh(self.repo, "worktree", "add", "-q", "-b", br, wt)
            if name == "a":
                self.fk1 = dated_commit(wt, "fk1", T + 300)
            else:
                self.fm1 = dated_commit(wt, "fm1", T + 500)
                self.fm2 = dated_commit(wt, "fm2", T + 600)
        for name in ("a", "b"):
            sh(self.repo, "worktree", "remove", "--force", os.path.join(self.gone, name))
        when = {"GIT_AUTHOR_DATE": "%d +0000" % (T + 700), "GIT_COMMITTER_DATE": "%d +0000" % (T + 700)}
        subprocess.run(["git", "-C", self.repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'feat-merged'", "feat-merged"],
                       env=dict(ENV, **when), check=True, capture_output=True)
        sh(self.repo, "branch", "-q", "-d", "feat-merged")
        self.claude = os.path.join(base, "claude")
        d = os.path.join(self.claude, "-some-other-project")  # not named after this repo at all
        os.makedirs(d)

        def line(mid, ts, cwd, branch, n):
            m = {"type": "assistant", "cwd": cwd, "timestamp": iso(ts),
                 "message": {"id": mid, "usage": {"input_tokens": n, "cache_read_input_tokens": 0,
                                                  "cache_creation_input_tokens": 0, "output_tokens": 0}}}
            if branch:
                m["gitBranch"] = branch
            return json.dumps(m)
        z = lambda n: os.path.join(self.gone, n)
        rows = [line("r1", T + 250, z("a"), "feat-keep", 1),       # surviving branch -> fk1
                line("r2", T + 550, z("b"), "feat-merged", 10),    # deleted+merged -> second-parent chain -> fm2
                line("r3", T + 150, z("c"), "main", 100),          # glob only, generic branch -> gone-worktree
                line("r4", T + 150, z("d"), None, 1000),           # glob only, no branch -> gone-worktree
                line("r5", T + 150, os.path.join(base, "else", "x"), "main", 7777),   # other repo, same name 'main'
                line("r6", T + 150, os.path.join(base, "else", "y"), "zzz-unknown", 8888),
                line("r7", T + 270, os.path.join(base, "else", "w"), "feat-keep", 10000)]  # branch evidence, no glob
        for i in (2, 3):  # glob-only workers sit in project folders named after their own cwd
            dd = os.path.join(self.claude, re.sub(r"[^A-Za-z0-9]", "-", z("cd"[i - 2])))
            os.makedirs(dd)
            with open(os.path.join(dd, "s.jsonl"), "w") as f:
                f.write(rows[i] + "\n")
        rows = rows[:2] + rows[4:]
        with open(os.path.join(d, "s.jsonl"), "w") as f:
            f.write("\n".join(rows) + "\n")
        self.codex = os.path.join(base, "codex")
        os.makedirs(self.codex)
        with open(os.path.join(self.codex, "rollout-x.jsonl"), "w") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"cwd": z("e")}}) + "\n")
            f.write(json.dumps({"timestamp": iso(T + 150), "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 100000, "cached_input_tokens": 0, "output_tokens": 0}}}}) + "\n")
        self.src = {"claude": self.claude, "codex": self.codex, "opencode": os.path.join(base, "none.db")}

    def run_tokens(self, **kw):
        return tokens.tokens(self.repo, sources=self.src, **kw)

    def test_gone_worktrees(self):
        r = self.run_tokens(worktree_roots=[os.path.join(self.gone, "*")])
        self.assertEqual(r["commits"][self.fk1]["claude"]["input"], 1 + 10000)
        self.assertEqual(r["commits"][self.fm2]["claude"]["input"], 10)
        gone = r["unattributed"]["reasons"]["gone-worktree"]
        self.assertEqual(gone["claude"]["input"], 100 + 1000)
        self.assertEqual(gone["codex"]["input"], 100000)
        self.assertEqual(list(r["unattributed"]["reasons"]), ["gone-worktree"])
        self.assertEqual(r["total"]["claude"]["input"], 1 + 10 + 100 + 1000 + 10000)  # 7777 / 8888 never claimed
        self.assertNotIn("codex", self.run_tokens()["total"])  # no glob configured: path alone is not enough

    def test_without_glob_only_branch_evidence(self):
        r = self.run_tokens(worktree_roots=[])
        self.assertEqual(r["total"]["claude"]["input"], 1 + 10 + 10000)
        self.assertEqual(r["unattributed"]["by_tool"], {})

    def test_config_file(self):
        cfg = os.path.join(self.base, "outside.yaml")
        with open(cfg, "w") as f:
            f.write('worktree_roots:\n  - "%s"\n' % os.path.join(self.gone, "*"))
        self.assertIn("gone-worktree", self.run_tokens(config=cfg)["unattributed"]["reasons"])


class TokensMcp(unittest.TestCase):
    def test_round_trip(self):
        fx = Fixture()
        env = dict(ENV, PYTHONPATH=helper.SRC, **fx.env)
        p = subprocess.Popen([sys.executable, "-m", "dropviz", "mcp", fx.repo], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

        def rpc(i, method, params=None):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
            p.stdin.flush()
            return json.loads(p.stdout.readline())
        try:
            rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
            self.assertIn("tokens", [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]])
            r = rpc(3, "tools/call", {"name": "tokens", "arguments": {"limit": 2}})["result"]
            self.assertFalse(r["isError"])
            res = json.loads(r["content"][0]["text"])
            self.assertEqual(len(res["commits"]), 2)
            self.assertGreater(res["commits_omitted"], 0)
            self.assertEqual(res["total"]["opencode"]["input"], 7)
            r = rpc(4, "tools/call", {"name": "tokens", "arguments": {"commit": "nope"}})["result"]
            self.assertTrue(r["isError"])
        finally:
            p.stdin.close()
            p.wait(timeout=10)
            p.stdout.close()
            p.stderr.close()


if __name__ == "__main__":
    unittest.main()


class IndexCache(unittest.TestCase):
    """The per-file transcript index (speed-up only): warm == cold, appends resume, a half line waits."""

    def setUp(self):
        self.fx = Fixture()
        self.cache = tempfile.mkdtemp(prefix="dz-idx-")
        p = mock.patch.dict(os.environ, {"DROPVIZ_CACHE_DIR": self.cache})
        p.start()
        self.addCleanup(p.stop)

    def run_tokens(self):
        return tokens.tokens(self.fx.repo, sources={"claude": self.fx.claude, "codex": self.fx.codex,
                                                    "opencode": self.fx.db})

    def test_warm_equals_cold_and_cache_written(self):
        cold = self.run_tokens()
        self.assertTrue(os.path.isfile(os.path.join(self.cache, "claude-index.json")))
        self.assertEqual(self.run_tokens(), cold)

    def test_append_and_half_line(self):
        first = self.run_tokens()
        path = next(os.path.join(dp, f) for dp, _, fs in os.walk(self.fx.claude) for f in fs if f.endswith(".jsonl"))
        line = json.dumps({"type": "assistant", "cwd": self.fx.wa, "gitBranch": "feat", "timestamp": iso(T + 450),
                           "message": {"id": "new-1", "usage": {"input_tokens": 7, "output_tokens": 3}}})
        with open(path, "a") as f:
            f.write(line[:30])  # half-written line: ignored for now
        self.assertEqual(self.run_tokens()["total"], first["total"])
        with open(path, "a") as f:
            f.write(line[30:] + "\n")
        t = self.run_tokens()["total"]["claude"]
        self.assertEqual(t["input"], first["total"]["claude"]["input"] + 7)
        self.assertEqual(t["output"], first["total"]["claude"]["output"] + 3)
        self.assertEqual(self.run_tokens()["total"]["claude"], t)

    def test_truncated_file_is_reindexed(self):
        self.run_tokens()
        path = next(os.path.join(dp, f) for dp, _, fs in os.walk(self.fx.claude) for f in fs if f.endswith(".jsonl"))
        with open(path, "w") as f:
            f.write("")
        self.assertNotIn("claude", self.run_tokens()["total"])

"""Models from git trailers, the opt-in done rules, the replay that grows, no e-mails in output, speed on a big history."""
import http.server
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest

from tests.helper import AT, ENV, sh, write
from dropviz import core, donerule, timelapse, trailers, treemap, web

CFG = """name: "fixture"
default_branch: "main"
features:
  - id: "alpha"
    name: "Alpha"
    paths: ["alpha/"]
    area: "Core"
  - id: "beta"
    name: "Beta"
    paths: ["beta/"]
    area: "Core"
"""


def commit_at(repo, msg, rel, text, when):
    write(repo, rel, text)
    env = dict(ENV, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(["git", "-C", repo, "add", "-A"], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", msg], env=env, check=True, capture_output=True)
    return sh(repo, "rev-parse", "HEAD")


def fixture(done_cfg=""):
    d = tempfile.mkdtemp(prefix="dropviz-trailers-")
    sh(d, "init", "-q", "-b", "main")
    write(d, "dropviz.yaml", CFG + done_cfg)
    e = "noreply" + AT + "anthropic.com"
    commit_at(d, "alpha one\n\nCo-Authored-By: Claude Opus 5.5 <%s>" % e, "alpha/a.py", "a\nb\nc\n", "2026-01-01T10:00:00Z")
    commit_at(d, "alpha two\n\nco-authored-by: claude-sonnet-4-5-20250929 <%s>\n\nGenerated with [Claude Code](https://x)" % e,
              "alpha/b.py", "a\nb\n", "2026-01-02T10:00:00Z")
    commit_at(d, "human only\n\nCo-authored-by: Dana Human <dana%sexample.invalid>" % AT, "alpha/c.py", "x\n", "2026-01-03T10:00:00Z")
    commit_at(d, "beta late\n\nAssisted-by: Codex:gpt-5.1-codex [rg]", "beta/a.py", "a\nb\nc\nd\n", "2026-02-01T10:00:00Z")
    commit_at(d, "two models\n\nCo-Authored-By: Claude Opus 5.5 <%s>\nCo-Authored-By: GPT-5.1 Codex <x%sopenai.com>" % (e, AT),
              "beta/b.py", "a\nb\n", "2026-02-02T10:00:00Z")
    commit_at(d, "monet\n\nCo-authored-by: Claude Monet <monet%sexample.invalid>" % AT, "beta/c.py", "m\n", "2026-02-03T10:00:00Z")
    return d


class Parse(unittest.TestCase):
    def test_variants(self):
        e = "n" + AT + "anthropic.com"
        cases = {
            "Co-Authored-By: Claude Opus 5.5 <%s>" % e: ["Claude Opus 5.5"],
            "co-authored-by: CLAUDE-OPUS-4-5-20251101 <%s>" % e: ["Claude Opus 4.5"],
            "Co-Authored-By: Claude Sonnet 4.5 (1M context) <%s>" % e: ["Claude Sonnet 4.5"],
            "Co-Authored-By: Claude <%s>" % e: ["Claude"],
            "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)": ["Claude Code"],
            "Co-Authored-By: Claude Opus 5.5 <%s>\nGenerated with [Claude Code](x)" % e: ["Claude Opus 5.5"],
            "Assisted-by: Codex:gpt-5.1-codex [rg]": ["GPT-5.1 Codex"],
            "Assisted-by: SomeAgent 2": ["SomeAgent 2"],
            "Co-authored-by: GPT-5.1 Codex <a%sopenai.com>" % AT: ["GPT-5.1 Codex"],
            "Co-authored-by: Gemini 2.5 Pro <a%sb.c>" % AT: ["Gemini 2.5 Pro"],
            "Co-authored-by: Dana Human <d%sx.y>" % AT: [],
            "Co-authored-by: Claude Monet <m%sx.y>" % AT: [],
            "Assisted-by: bot%sevil.example" % AT: [],
            "just text mentioning co-authored-by in prose": [],
        }
        for text, want in cases.items():
            self.assertEqual(trailers.models_in_message("subject\n\n" + text), want, text)

    def test_no_email_in_any_name(self):
        for text in ("Co-Authored-By: Claude Opus 5.5 <a%sb.c>" % AT, "Assisted-by: x%sy.z" % AT):
            for m in trailers.models_in_message(text):
                self.assertNotIn(AT, m)


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = fixture()
        cls.state = core.compute(cls.repo)
        cls.tr = trailers.summarize(cls.repo, cls.state)

    def test_counts_per_model_and_feature(self):
        bm = self.tr["by_model"]
        self.assertEqual(set(bm), {"Claude Opus 5.5", "Claude Sonnet 4.5", "GPT-5.1 Codex"})
        self.assertEqual(bm["Claude Sonnet 4.5"]["commits"], 1)
        # two-model commit: counted once for each, its 2 changed lines shared
        beta = self.tr["features"]["beta"]
        self.assertEqual(beta["GPT-5.1 Codex"]["commits"], 2)
        self.assertEqual(beta["Claude Opus 5.5"]["commits"], 1)
        self.assertAlmostEqual(beta["Claude Opus 5.5"]["lines"], 1.0)
        self.assertAlmostEqual(beta["GPT-5.1 Codex"]["lines"], 5.0)
        self.assertEqual(set(self.tr["features"]["alpha"]), {"Claude Opus 5.5", "Claude Sonnet 4.5"})
        self.assertLess(self.tr["features"]["alpha"]["Claude Opus 5.5"]["first"], self.tr["features"]["alpha"]["Claude Sonnet 4.5"]["first"])
        self.assertEqual(set(self.tr["areas"]["Core"]), set(bm))
        self.assertEqual(self.tr["commits_with_model"], 4)

    def test_strict_is_default_and_trailers_do_not_make_done(self):
        self.assertEqual(self.state["done_rule"], "strict")
        self.assertTrue(all(f["status"] != "done" for f in self.state["features"]))

    def test_models_view_and_no_email(self):
        ctx = web_ctx(self.repo, self.state)
        lay = treemap.layout(self.state, 900, 500, "models", ctx=ctx)
        tiles = [n for n in lay["nodes"] if n["kind"] == "feature"]
        self.assertTrue(all(n.get("bands") for n in tiles))
        self.assertAlmostEqual(sum(b["frac"] for b in tiles[0]["bands"]), 1.0, places=2)
        labels = [x["label"] for x in lay["legend"]]
        self.assertIn("Claude Opus 5.5", labels)
        self.assertTrue(any("trailers" in x for x in labels))
        blob = json.dumps(lay) + json.dumps({k: v for k, v in self.tr.items() if k != "rows"})
        for bad in (AT + "anthropic", AT + "openai", AT + "example", "noreply"):
            self.assertNotIn(bad, blob)
        # tokens are never invented
        self.assertNotIn("tokens", json.dumps({k: v for k, v in self.tr.items() if k != "rows"}))

    def test_huge_history_counts_files_not_lines(self):
        old = trailers.T["max_numstat_commits"]
        trailers.T["max_numstat_commits"] = 2
        try:
            tr = trailers.summarize(self.repo, self.state)
        finally:
            trailers.T["max_numstat_commits"] = old
        self.assertEqual(tr["unit"], "files")
        self.assertEqual(self.tr["unit"], "lines")
        self.assertEqual(tr["features"]["beta"]["GPT-5.1 Codex"]["lines"], 1.5)   # one file per commit; the two-model commit shares it
        lay = treemap.layout(self.state, 900, 500, "models", ctx=dict(web_ctx(self.repo, self.state), trailers=dict(tr, colors=trailers.colors(tr))))
        self.assertTrue(any("files" in x["label"] for x in lay["legend"]))

    def test_replay_models_are_cut_at_the_day(self):
        early = trailers.summarize(self.repo, self.state, until=timelapse._day_end("2026-01-31"), rows=self.tr["rows"])
        self.assertNotIn("beta", early["features"])
        self.assertNotIn("GPT-5.1 Codex", early["by_model"])


def web_ctx(repo, state):
    cache = web.Cache(repo)
    return web.trailer_ctx(cache, state) and {"trailers": web.trailer_ctx(cache, state), "done_rule": state["done_rule"]}


class Replay(unittest.TestCase):
    def test_feature_does_not_exist_before_its_first_commit(self):
        repo = fixture()
        st = core.compute(repo)
        tl = timelapse.frames(repo, st)
        days = tl["days"]
        i_jan, i_mid, i_last = days.index("2026-01-02"), days.index("2026-01-20"), len(days) - 1
        self.assertEqual(tl["frames"][i_jan]["s"]["beta"], "x")
        self.assertEqual(tl["frames"][i_mid]["s"]["beta"], "x")
        self.assertNotEqual(tl["frames"][i_jan]["s"]["alpha"], "x")
        self.assertNotEqual(tl["frames"][i_last]["s"]["beta"], "x")
        early = timelapse.state_at(st, tl["frames"][i_jan])
        lay = treemap.layout(early, 900, 500, "progress")
        ids = {n["key"] for n in lay["nodes"]}
        self.assertIn("x:beta", ids)            # an empty slot, not a coloured tile
        self.assertNotIn("f:beta", ids)
        self.assertIn("f:alpha", ids)
        late = treemap.layout(timelapse.state_at(st, tl["frames"][i_last]), 900, 500, "progress")
        self.assertIn("f:beta", {n["key"] for n in late["nodes"]})
        self.assertNotIn("x:beta", {n["key"] for n in late["nodes"]})


class DoneRules(unittest.TestCase):
    def test_merged_rule(self):
        repo = fixture('done: "merged"\n')
        st = core.compute(repo)
        self.assertEqual(st["done_rule"], "merged")
        self.assertTrue(all(f["status"] == "done" for f in st["features"]))
        lay = treemap.layout(st, 600, 400, "progress")
        self.assertTrue(any("merged" in x["label"] and "git" in x["label"] for x in lay["legend"]))

    def test_flag_overrides_config_and_bad_value_is_refused(self):
        repo = fixture()
        os.environ["DROPVIZ_DONE"] = "merged"
        try:
            self.assertEqual(core.compute(repo)["done_rule"], "merged")
            os.environ["DROPVIZ_DONE"] = "nonsense"
            with self.assertRaises(ValueError):
                core.compute(repo)
        finally:
            del os.environ["DROPVIZ_DONE"]

    def test_strict_legend_names_the_rule(self):
        repo = fixture()
        lay = treemap.layout(core.compute(repo), 600, 400, "progress")
        self.assertTrue(any("strict" in x["label"] for x in lay["legend"]))

    def test_unmerged_newer_work_blocks_merged_rule(self):
        repo = fixture('done: "merged"\n')
        sh(repo, "checkout", "-q", "-b", "claude/beta-more")
        commit_at(repo, "wip", "beta/wip.py", "w\n", "2026-03-01T10:00:00Z")
        sh(repo, "checkout", "-q", "main")
        st = core.compute(repo)
        by = {f["id"]: f["status"] for f in st["features"]}
        self.assertEqual(by["alpha"], "done")
        self.assertEqual(by["beta"], "in-progress")

    def test_merged_ci_with_a_fake_github(self):
        repo = fixture('done: "merged+ci"\n')
        sh(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
        shas = {f: sh(repo, "log", "-1", "--format=%H", "--", f + "/") for f in ("alpha", "beta")}
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path)
                ok = shas["alpha"] in self.path
                if "check-runs" in self.path:
                    body = {"check_runs": [{"conclusion": "success" if ok else "failure"}]}
                else:
                    body = {"state": "success"}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-RateLimit-Remaining", "50")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["DROPVIZ_GITHUB_API"] = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            st = core.compute(repo)
            by = {f["id"]: f for f in st["features"]}
            self.assertEqual(by["alpha"]["status"], "done")
            self.assertIn("checks passed", by["alpha"]["why"][0])
            self.assertNotEqual(by["beta"]["status"], "done")   # its checks failed
            n = len(seen)
            core.compute(repo)
            self.assertEqual(len(seen), n)                       # second run: answered from the cache
        finally:
            del os.environ["DROPVIZ_GITHUB_API"]
            srv.shutdown()

    def test_merged_ci_without_a_github_origin_is_never_done(self):
        repo = fixture('done: "merged+ci"\n')
        st = core.compute(repo)
        self.assertTrue(all(f["status"] != "done" for f in st["features"]))
        self.assertIn("no GitHub origin", st["features"][0]["why"][0])


class Drafter(unittest.TestCase):
    HEADINGS = ["Features", "Everything in the box", "Get started", "Security", "Downloads", "Star history",
                "License and credits", "Community packages", "Documentation", "Install", "Contributing"]

    def repo(self, code=True, extra=""):
        d = tempfile.mkdtemp(prefix="dropviz-draft-")
        sh(d, "init", "-q", "-b", "main")
        text = "# Demo\n\n" + "".join("## %s\ntext\n\n" % h for h in self.HEADINGS) + extra
        write(d, "README.md", text)
        if code:
            write(d, "crates/io/a.rs", "x\n")
            write(d, "crates/ui/a.rs", "x\n")
            write(d, "tools/t.py", "x\n")
        sh(d, "add", "-A")
        sh(d, "commit", "-q", "-m", "init")
        return d

    def test_boilerplate_headings_are_not_features(self):
        ids = [f["id"] for f in core.draft_features(self.repo())]
        self.assertEqual([i for i in ids if i.startswith("readme-")], [])
        self.assertIn("crates-io", ids)

    def test_heading_that_names_a_code_folder_gets_its_paths(self):
        fs = core.draft_features(self.repo(extra="## The `crates/io` layer\ntext\n\n## Tools\ntext\n\n## Security\ntext\n"))
        r = {f["id"]: f for f in fs if f["id"].startswith("readme-")}
        self.assertEqual(sorted(r), ["readme-the-crates-io-layer", "readme-tools"])
        self.assertEqual(r["readme-tools"]["paths"], ["tools/"])

    def test_a_repo_without_code_folders_keeps_its_non_boilerplate_headings(self):
        ids = [f["id"] for f in core.draft_features(self.repo(code=False, extra="## Architecture\ntext\n"))]
        self.assertIn("readme-architecture", ids)
        self.assertNotIn("readme-security", ids)
        self.assertNotIn("readme-downloads", ids)


class Speed(unittest.TestCase):
    def test_many_commits(self):
        """3,000 trailer commits through one git call; the time is printed (the 13k-commit repo is measured by hand)."""
        d = tempfile.mkdtemp(prefix="dropviz-speed-")
        sh(d, "init", "-q", "-b", "main")
        write(d, "dropviz.yaml", CFG)
        write(d, "alpha/a.py", "x\n")
        sh(d, "add", "-A")
        sh(d, "commit", "-q", "-m", "init")
        stream = []
        for i in range(3000):
            stream.append("commit refs/heads/main\ncommitter T <t%sx.y> %d +0000\ndata <<M\nc%d\n\nCo-Authored-By: Claude Opus 5.5 <n%sa.b>\nM\n"
                          "M 100644 inline alpha/f%d.py\ndata 2\nx\n\n" % (AT, 1700000000 + i, i, AT, i % 50))
        subprocess.run(["git", "-C", d, "fast-import", "--quiet", "--force"], input="".join(stream).encode(), check=True,
                       env=ENV, capture_output=True)
        sh(d, "reset", "-q", "--hard", "main")
        st = core.compute(d)
        t0 = time.time()
        tr = trailers.summarize(d, st)
        took = time.time() - t0
        print("trailers: 3000 commits parsed and summed in %.2f s" % took)
        self.assertEqual(tr["by_model"]["Claude Opus 5.5"]["commits"], 3000)
        self.assertLess(took, 20)


if __name__ == "__main__":
    unittest.main()

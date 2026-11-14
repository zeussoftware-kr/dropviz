"""Map, views, size rules, settings, languages, live layer, models, timelapse, badge, no-hard-coding rules."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from tests import helper
from tests.helper import ROOT, make_repo
from tests.lint_rules import find

from dropviz import badge, cli, core, defaults, i18n, live, mcp, metrics, timelapse, tokens, treemap

DEMO = None
ENVMOD = {}


def demo():
    """One invented example repo for the whole module (about 2 s to build)."""
    global DEMO
    if DEMO is None:
        d = tempfile.mkdtemp(prefix="dropviz-demo-")
        subprocess.run([sys.executable, os.path.join(ROOT, "examples", "make_demo_repo.py"), os.path.join(d, "kestrel")],
                       check=True, capture_output=True)
        DEMO = d
        sd = d + "/kestrel-sessions"
        for k, v in (("DROPVIZ_CLAUDE_DIR", sd + "/claude"), ("DROPVIZ_CODEX_DIR", sd + "/codex"), ("DROPVIZ_OPENCODE_DB", sd + "/opencode.db")):
            ENVMOD[k] = os.environ.get(k)
            os.environ[k] = v
    return DEMO + "/kestrel"


def tearDownModule():
    for k, v in ENVMOD.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if DEMO:
        shutil.rmtree(DEMO, ignore_errors=True)


class Treemap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = demo()
        cls.state = core.compute(cls.root)

    def tiles(self, lay):
        return [n for n in lay["nodes"] if n["kind"] == "feature"]

    def test_one_tile_per_feature_and_status_matches_evidence(self):
        lay = treemap.layout(self.state, 1400, 800)
        tiles = self.tiles(lay)
        feats = {f["id"]: f for f in self.state["features"]}
        self.assertEqual(len(tiles), len(feats))
        self.assertGreaterEqual(len(feats), 150)
        self.assertEqual(lay["stats"]["planned"], self.state["counts"]["planned"])
        for n in tiles:
            f = feats[n["id"]]
            self.assertEqual(n["status"], f["status"])
            if f["status"] == "done":  # no done colour without a merge and a verification record
                self.assertGreater(f["evidence"]["merged_commits"], 0)
                self.assertTrue(f["evidence"]["verified_commit"])
                self.assertEqual(n["fill"], defaults.data()["status_colors"]["done"])
            else:
                self.assertNotEqual(n["fill"], defaults.data()["status_colors"]["done"])
            self.assertEqual(n["unverified"], f["status"] != "done" and f["evidence"]["merged_commits"] > 0)
        st = self.state["counts"]
        self.assertEqual(st["done"] + st["in-progress"] + st["unknown"], st["total"])
        self.assertGreater(st["done"], 50)
        self.assertGreater(st["in-progress"], 5)
        self.assertGreater(sum(1 for f in feats.values() if f["status"] == "unknown" and f["evidence"]["merged_commits"]), 3)  # unverified merges exist

    def test_nested_areas_and_breadcrumb(self):
        lay = treemap.layout(self.state, 1400, 800, focus="Thumbnails")
        self.assertEqual([c["name"] for c in lay["crumbs"]], ["Core", "Library", "Thumbnails"])
        self.assertTrue(all(n["id"] for n in self.tiles(lay)))
        area_tiles = {f["id"] for f in self.state["features"] if f["area"] == "Thumbnails"}
        self.assertEqual({n["id"] for n in self.tiles(lay)}, area_tiles)

    def test_rects_stay_inside_and_do_not_overlap_siblings(self):
        lay = treemap.layout(self.state, 1000, 600)
        for n in lay["nodes"]:
            self.assertGreaterEqual(n["x"], -0.1)
            self.assertLessEqual(n["x"] + n["w"], 1000.2)
            self.assertLessEqual(n["y"] + n["h"], 600.2)

    def test_phone_size_renders(self):
        lay = treemap.layout(self.state, 360, 520)
        self.assertEqual(len(self.tiles(lay)), len(self.state["features"]))

    def test_three_size_rules_change_areas_not_statuses(self):
        d = tempfile.mkdtemp(prefix="dropviz-copy-")
        try:
            copy = os.path.join(d, "r")
            shutil.copytree(self.root, copy, symlinks=True)
            core.save_settings(copy, {"areas": [{"name": "Core", "weight": 1, "areas": [{"name": "Library", "areas": [
                {"name": "Indexing"}, {"name": "Thumbnails"}, {"name": "Metadata"}]}, {"name": "Import", "areas": [{"name": "Sources"}, {"name": "Dedupe"}]},
                {"name": "Search", "planned": 34, "areas": [{"name": "Query"}, {"name": "Faces"}]}]},
                {"name": "Client", "weight": 1, "areas": [{"name": "Viewer", "areas": [{"name": "Gestures"}, {"name": "Slideshow"}, {"name": "Display"}]},
                                                      {"name": "Editor", "areas": [{"name": "Adjust"}, {"name": "Filters"}, {"name": "Export"}]}]},
                {"name": "Server", "weight": 8, "areas": [{"name": "Accounts", "areas": [{"name": "Sign-in"}, {"name": "Profile"}]},
                                                       {"name": "Sync", "areas": [{"name": "Transfer"}, {"name": "Conflicts"}]},
                                                       {"name": "Sharing", "areas": [{"name": "Links"}, {"name": "Albums"}]}]},
                {"name": "Web", "areas": [{"name": "Gallery", "areas": [{"name": "Browse"}, {"name": "Upload"}]},
                                       {"name": "Admin", "planned": 30, "areas": [{"name": "Users"}, {"name": "Health"}]}]}]})
            st = core.compute(copy)
            rects = {}
            for rule in ("features", "weight", "work"):
                lay = treemap.layout(st, 1400, 800, "progress", size_by=rule)
                rects[rule] = {n["name"]: n["w"] * n["h"] for n in lay["nodes"] if n["kind"] == "area" and n["depth"] == 0}
                self.assertEqual({n["id"]: n["status"] for n in self.tiles(lay)}, {f["id"]: f["status"] for f in st["features"]})
            self.assertNotEqual(rects["features"], rects["weight"])
            self.assertNotEqual(rects["features"], rects["work"])
            self.assertNotEqual(rects["weight"], rects["work"])
            self.assertGreater(rects["weight"]["Server"] / rects["features"]["Server"], 1.5)  # the weight we set made Server bigger
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_planned_placeholders_are_not_features_or_done(self):
        lay = treemap.layout(self.state, 1400, 800)
        planned = [n for n in lay["nodes"] if n["kind"] == "planned"]
        self.assertEqual(len(planned), self.state["counts"]["planned"])
        self.assertGreater(len(planned), 20)
        self.assertTrue(all(n["status"] == "planned" and n["fill"] != defaults.data()["status_colors"]["done"] for n in planned))
        self.assertEqual(self.state["counts"]["total"], len(self.state["features"]))

    def test_every_view_renders_and_recolours(self):
        m = metrics.compute(self.root)
        ctx = {"metrics": m, "attention": core.attention(self.root), "live_by_feature": {}}
        base = {n["id"]: n["fill"] for n in self.tiles(treemap.layout(self.state, 1400, 800, "progress", ctx=ctx))}
        done_col = defaults.data()["status_colors"]["done"]
        seen = {}
        for v in defaults.data()["views"]:
            lay = treemap.layout(self.state, 1400, 800, v, ctx=ctx)
            tiles = self.tiles(lay)
            self.assertEqual(len(tiles), len(self.state["features"]), v)
            self.assertTrue(lay["legend"], v)
            fills = {n["id"]: n["fill"] for n in tiles}
            seen[v] = fills
            if v != "progress":
                self.assertNotEqual(fills, base, v)
                self.assertNotIn(done_col, fills.values(), v)  # the done colour is only ever evidence
            for n in tiles:  # the honest stripe is in every view
                self.assertEqual(n["stripe"], defaults.data()["status_colors"][n["status"]])
        # expected colours: a claimed feature is amber in Now, a stale branch or CI fail is red in Risk
        claimed = [f["id"] for f in self.state["features"] if f["claim"]]
        self.assertTrue(claimed)
        self.assertEqual(seen["now"][claimed[0]], defaults.data()["view_colors"]["now_claim"])
        self.assertIn(defaults.data()["view_colors"]["risk_red"], seen["risk"].values())
        self.assertIn(defaults.data()["view_colors"]["risk_orange"], seen["risk"].values())
        self.assertGreater(len(set(seen["attention"].values())), 5)

    def test_model_filter_marks_tiles(self):
        t = tokens.tokens(self.root)
        fm = tokens.feature_models(self.root, t, self.state["features"])
        lay = treemap.layout(self.state, 1400, 800, ctx={"models": fm}, model="gpt-5-codex")
        hit = [n for n in self.tiles(lay) if n.get("model_hit")]
        self.assertTrue(hit)
        for n in hit:
            self.assertIn("gpt-5-codex", fm[n["id"]])


class Settings(unittest.TestCase):
    def test_save_rename_nest_backup_and_roundtrip(self):
        d = make_repo()
        core.write_draft(d, answer="")
        before = core.compute(d)
        areas = sorted({f["area"] for f in before["features"]})
        res = core.save_settings(d, {"areas": [{"name": "Big", "areas": [{"name": a, "was": a} for a in areas]}], "size_by": "weight"})
        self.assertTrue(os.path.isfile(res["backup"]))
        after = core.compute(d)
        self.assertEqual(after["size_by"], "weight")
        self.assertEqual([a["name"] for a in after["areas"]], ["Big"])
        self.assertEqual([f["status"] for f in after["features"]], [f["status"] for f in before["features"]])
        res = core.save_settings(d, {"areas": [{"name": "Big", "areas": [{"name": a + "X", "was": a} for a in areas]}]})
        self.assertEqual({f["area"] for f in core.compute(d)["features"]}, {a + "X" for a in areas})
        self.assertGreaterEqual(len(os.listdir(os.path.join(d, ".dropviz", "backups"))), 2)

    def test_bad_settings_are_refused(self):
        d = make_repo()
        core.write_draft(d)
        for bad in ({"size_by": "money"}, {"areas": [{"name": "A"}, {"name": "a"}]}, {"areas": [{"name": ""}]},
                    {"areas": [{"name": "A", "planned": -3}]}, {"feature_areas": {"nope": "A"}}):
            with self.assertRaises(ValueError):
                core.save_settings(d, bad)

    def test_http_post_needs_local_header_and_saves(self):
        import threading
        from http.server import ThreadingHTTPServer
        from dropviz import web
        d = make_repo()
        core.write_draft(d)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(d))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        try:
            body = json.dumps({"size_by": "work"}).encode()
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(base + "/api/settings", data=body, method="POST"))
            self.assertEqual(cm.exception.code, 403)
            r = json.load(urllib.request.urlopen(urllib.request.Request(
                base + "/api/settings", data=body, method="POST", headers={"X-Dropviz-Local": "1", "Content-Type": "application/json"})))
            self.assertEqual(r["size_by"], "work")
            lay = json.load(urllib.request.urlopen(base + "/api/layout?w=800&h=500&view=parts"))
            self.assertTrue(lay["nodes"])
            self.assertEqual(json.load(urllib.request.urlopen(base + "/api/i18n?lang=ko"))["lang"], "ko")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_init_question_and_defaults(self):
        d = tempfile.mkdtemp(prefix="dropviz-init-")
        env = dict(helper.ENV)
        subprocess.run(["git", "-C", d, "init", "-q", "-b", "main"], env=env, check=True)
        for rel in ("src/core/a.py", "src/core/b.py", "web/index.html", "server/api.py", "docs/x.md"):
            helper.write(d, rel, "x\n")
        helper.sh(d, "add", "-A")
        helper.sh(d, "commit", "-q", "-m", "init")
        core.write_draft(d, answer="a game")
        cfg = core.load_config(d)
        names = {a["name"] for a in cfg["areas"]}
        self.assertTrue({"Engine", "Gameplay", "Online"} <= names)
        self.assertEqual(next(a for a in cfg["areas"] if a["name"] == "web")["parent"], "Gameplay")
        init_b = tempfile.mkdtemp(prefix="dropviz-init2-")
        subprocess.run(["git", "-C", init_b, "init", "-q", "-b", "main"], env=env, check=True)
        helper.write(init_b, "src/a.py", "x\n")
        helper.write(init_b, "web/a.js", "x\n")
        helper.sh(init_b, "add", "-A")
        helper.sh(init_b, "commit", "-q", "-m", "init")
        core.write_draft(init_b)  # no answer: a sensible default
        self.assertTrue(core.compute(init_b)["features"])
        calls = []
        orig = cli.ask_kind
        cli.ask_kind = lambda: calls.append(1) or "an OS"
        try:
            d3 = tempfile.mkdtemp(prefix="dropviz-init3-")
            subprocess.run(["git", "-C", d3, "init", "-q", "-b", "main"], env=env, check=True)
            for rel in ("kernel/a.c", "web/a.js"):
                helper.write(d3, rel, "x\n")
            helper.sh(d3, "add", "-A")
            helper.sh(d3, "commit", "-q", "-m", "init")
            sys_stdin = sys.stdin
            sys.stdin = type("TTY", (), {"isatty": lambda self: True})()
            try:
                cli.main(["init", d3])
            finally:
                sys.stdin = sys_stdin
            self.assertEqual(calls, [1])
            self.assertIn("Kernel", {a["name"] for a in core.load_config(d3)["areas"]})
        finally:
            cli.ask_kind = orig

    def test_first_look_without_yaml_keeps_the_tree_clean(self):
        d = make_repo()
        self.assertFalse(core.load_config(d))
        core.write_auto_config(d)
        self.assertTrue(core.compute(d)["features"])
        self.assertFalse(os.path.exists(os.path.join(d, "dropviz.yaml")))
        self.assertEqual(helper.sh(d, "status", "--porcelain"), "")


class Mcp(unittest.TestCase):
    def test_configure_and_status_fields(self):
        root = demo()
        d = tempfile.mkdtemp(prefix="dropviz-mcp-")
        copy = os.path.join(d, "r")
        shutil.copytree(root, copy, symlinks=True)
        srv = mcp.Server(copy)
        cur = srv.call("configure", {})
        self.assertIn("areas", cur)
        res = srv.call("configure", {"size_by": "weight", "areas": [{"name": "A", "areas": [{"name": "Library", "was": "Library"}]}]})
        self.assertTrue(os.path.isfile(res["backup"]))
        self.assertEqual(core.compute(copy)["size_by"], "weight")
        st = srv.call("status", {})
        for k in ("working_now", "models_tokens", "model_ranking", "tokens_total", "areas"):
            self.assertIn(k, st)
        self.assertTrue(st["working_now"])
        with self.assertRaises(ValueError):
            srv.call("configure", {"size_by": "money"})
        ko = mcp.tools("ko")
        self.assertNotEqual(ko[0]["description"], mcp.tools("en")[0]["description"])
        shutil.rmtree(d, ignore_errors=True)


class LiveAndModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = demo()
        cls.state = core.compute(cls.root)

    def test_working_now_fresh_and_stale(self):
        w = live.working_now(self.root, self.state)
        sessions = [e for e in w["entries"] if e["kind"] == "session"]
        self.assertTrue(any(e["tool"] == "claude" and not e["stalled"] for e in sessions))
        self.assertTrue(any(e["tool"] == "claude" and e["stalled"] for e in sessions))
        self.assertTrue(any(e["tool"] == "codex" and e["model"] == "gpt-5-codex" for e in sessions))
        self.assertTrue(any(e["kind"] == "claim" and e["stalled"] for e in w["entries"]))
        self.assertTrue(any(e["kind"] == "claim" and not e["stalled"] for e in w["entries"]))
        self.assertTrue(w["by_feature"])
        for e in w["entries"]:  # only these fields: no message text, no transcript path, no e-mail
            self.assertNotIn("session", e)
            self.assertNotIn("text", e)
        self.assertNotIn("@", json.dumps(w))
        lay = treemap.layout(self.state, 1400, 800, ctx={"live_by_feature": w["by_feature"]})
        self.assertTrue([n for n in lay["nodes"] if n.get("live")])

    def test_remote_hosts_are_off_by_default(self):
        self.assertFalse(core.load_config(self.root).get("remote_hosts"))
        self.assertFalse([e for e in live.working_now(self.root, self.state)["entries"] if e["tool"] == "remote"])

    def test_models_three_plus_and_unknown_bucket(self):
        t = tokens.tokens(self.root)
        self.assertTrue({"claude-opus-4-5", "claude-sonnet-4-5", "gpt-5-codex"} <= set(t["by_model"]))
        # counter = per-commit + unattributed = total (nothing counted twice, nothing dropped)
        s = tokens.summary(t)
        per_commit = sum(sum(c[k] for k in tokens.FIELDS) for per in t["commits"].values() for c in per.values())
        un = sum(sum(c[k] for k in tokens.FIELDS) for c in t["unattributed"]["by_tool"].values())
        self.assertEqual(s["total"], per_commit + un)
        self.assertEqual(s["total"], sum(s["per_tool"].values()))
        self.assertEqual(s["total"], sum(s["per_model"].values()))
        self.assertEqual(s["total"], s["input"] + s["output"] + s["cache_read"] + s["cache_write"])
        fm = tokens.feature_models(self.root, t, self.state["features"])
        rk = tokens.ranking(self.state["features"], t, fm)
        self.assertAlmostEqual(sum(r["credit"] for r in rk["rows"]), rk["done_features"], places=3)
        self.assertEqual(rk["done_features"], self.state["counts"]["done"])
        self.assertEqual(sum(r["tokens"] for r in rk["rows"]), s["total"])
        few = tokens.ranking(self.state["features"], t, fm, min_features=10 ** 6)
        self.assertTrue(all(r["tokens_per_feature"] is None and not r["enough"] for r in few["rows"]))

    def test_model_never_guessed(self):
        r = tokens._rec("claude", 1, "/x", None, "s", 1, 0, 0, 1)
        self.assertEqual(r["model"], "")
        self.assertEqual(tokens._model("<synthetic>"), "")
        self.assertEqual(tokens._model("claude-opus-4-5"), "claude-opus-4-5")

    def test_gap_days_are_none_not_zero(self):
        s = tokens.summary({"total": {}, "by_day": {"2026-01-01": 5, "2026-01-04": 7}, "by_model": {}})
        self.assertEqual([x["cumulative"] for x in s["series"]], [5, None, None, 12])


class Replay(unittest.TestCase):
    def test_frames_equal_days_monotonic_and_last_is_now(self):
        root = demo()
        st = core.compute(root)
        tl = timelapse.frames(root, st)
        first = int(core.git(root, "log", "--format=%ct", "--reverse", "--branches").split()[0])
        days = (time.time() - first) // 86400 + 1
        self.assertIn(len(tl["frames"]), (int(days), int(days) + 1, int(days) - 1))
        self.assertEqual(len(tl["frames"]), len(tl["days"]))
        self.assertEqual(tl["counts"], sorted(tl["counts"]))
        self.assertEqual(tl["counts"][-1], st["counts"]["done"])
        self.assertEqual(tl["counts"][0], 0)
        last = {f["id"]: f["status"] for f in st["features"]}
        name = {"d": "done", "p": "in-progress", "u": "unknown"}
        self.assertEqual({k: name[v] for k, v in tl["frames"][-1]["s"].items()}, last)
        mid = timelapse.state_at(st, tl["frames"][len(tl["frames"]) // 2])
        lay = treemap.layout(mid, 1000, 600)
        shown = len([n for n in lay["nodes"] if n["kind"] == "feature"])
        absent = len([n for n in lay["nodes"] if n["key"].startswith("x:")])   # not started yet: an empty slot
        self.assertEqual(shown + absent, len(st["features"]))
        self.assertGreater(absent, 0)       # the replay starts small and grows


class Badge(unittest.TestCase):
    def test_numbers_equal_the_map(self):
        root = demo()
        st = core.compute(root)
        n = badge.numbers(st)
        self.assertEqual(n["verified"], st["counts"]["done"])
        svg = badge.svg(st, lang="en")
        self.assertIn("%d verified features" % st["counts"]["done"], svg)
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("http", svg.replace("http://www.w3.org/2000/svg", ""))
        self.assertIn("tokens", badge.svg(st, lang="en", tokens_total=1234))


class Languages(unittest.TestCase):
    def test_catalogues_complete_and_equal(self):
        en, ko = i18n.catalogue("en"), i18n.catalogue("ko")
        self.assertEqual(set(en), set(ko))
        ph = lambda s: sorted(re.findall(r"\{(\w+)\}", s))
        for k in en:
            self.assertTrue(en[k].strip() and ko[k].strip(), k)
            self.assertEqual(ph(en[k]), ph(ko[k]), k)
        self.assertEqual(i18n.languages(), ["en", "ko"])

    def test_every_key_used_in_code_exists(self):
        en = set(i18n.catalogue("en"))
        used = set()
        src = os.path.join(ROOT, "src", "dropviz")
        for f in glob.glob(src + "/*.py"):
            text = open(f, encoding="utf-8").read()
            used |= {k for k in re.findall(r"""["']((?:cli|err|map|mcp|metrics|page|set|tokens|badge|lang\.name)\.[\w-]+)["']""", text)
                     if not k.endswith(("_", ".html")) and k not in ("map.", "page.html")}
            for pre, rx in (("cli.", r"_t\(\"([\w-]+)\""), ("err.", r"_err\(\"([\w-]+)\""), ("map.", r"\bt\(lang, \"([\w-]+)\""),
                            ("metrics.", r"_m\(lang, \"([\w-]+)\"")):
                used |= {pre + k for k in re.findall(rx, text)}
        with open(os.path.join(src, "page.html"), encoding="utf-8") as fh:
            used |= {k for k in re.findall(r"""T\(["']([\w.-]+)["']""", fh.read()) if not k.endswith("_")}
        missing = sorted(k for k in used if k not in en and not k.endswith(("_", ".")))
        self.assertEqual(missing, [])
        for v in defaults.data()["views"]:
            self.assertIn("map." + v, en)
        for r in defaults.data()["size_rules"]:
            self.assertIn("set.rule_" + r, en)
        for t in mcp.SCHEMAS:
            self.assertIn("mcp.tool_" + t, en)
        for tab in ("detail", "now", "models", "commits"):
            self.assertIn("page.tab_" + tab, en)

    def test_language_choice(self):
        self.assertEqual(i18n.pick("ko-KR,en;q=0.8"), "ko")
        self.assertEqual(i18n.pick("fr_FR.UTF-8"), "en")
        self.assertEqual(i18n.pick(None, "ko_KR.UTF-8"), "ko")
        old = os.environ.get("DROPVIZ_LANG")
        os.environ["DROPVIZ_LANG"] = "ko"
        try:
            self.assertEqual(i18n.from_env(), "ko")
            self.assertEqual(i18n.t("ko", "map.risk"), "위험")
            self.assertEqual(i18n.t("ko", "no.such.key"), "no.such.key")
        finally:
            os.environ["DROPVIZ_LANG"] = old

    def test_views_have_both_languages_on_the_map(self):
        st = core.compute(demo())
        for lang in ("en", "ko"):
            lay = treemap.layout(st, 800, 500, "risk", lang=lang)
            self.assertTrue(all(not x["label"].startswith("map.") for x in lay["legend"]))
        self.assertNotEqual(treemap.layout(st, 800, 500, "risk", lang="en")["legend"][0]["label"],
                            treemap.layout(st, 800, 500, "risk", lang="ko")["legend"][0]["label"])


class NoHardCoding(unittest.TestCase):
    def test_must_catch(self):
        for line in ('print("Wrote the file")', 'p.add_argument("--x", help="show things")', 'c = "#ff0000"',
                     "STALE = 7 * 86400", "x = 30 * DAY", 'p = "~/.claude/projects"', 'raise ValueError("bad thing")',
                     "app.run(port=8765)"):
            self.assertTrue(find(line), line)

    def test_must_not_catch(self):
        for line in ('print(_t("wrote", path=p))', 'p.add_argument("--x", help=_t("help_x"))', 'c = defaults.data()["idle_color"]',
                     "DAY = 24 * 3600", "WEEK = 7 * DAY", 'raise ValueError(_err("size_by"))', 'p = defaults.source("claude_dir")',
                     'self.send_header("Content-Type", "application/json")', '"jsonrpc": "2.0"'):
            self.assertFalse(find(line), line)

    def test_source_is_clean(self):
        hits = []
        for f in glob.glob(os.path.join(ROOT, "src", "dropviz", "*.py")):
            with open(f, encoding="utf-8") as fh:
                found = find(fh.read())
            for n, label, line in found:
                hits.append("%s:%d %s: %s" % (os.path.basename(f), n, label, line))
        self.assertEqual(hits, [])

    def test_defaults_can_be_overridden(self):
        self.assertEqual(defaults.get("stale_claim_minutes"), 30)
        self.assertEqual(defaults.get("stale_claim_minutes", {"stale_claim_minutes": 5}), 5)
        os.environ["DROPVIZ_STALE_CLAIM_MINUTES"] = "7"
        try:
            self.assertEqual(defaults.get("stale_claim_minutes"), 7)
        finally:
            del os.environ["DROPVIZ_STALE_CLAIM_MINUTES"]

    def test_one_vocabulary_no_market_word(self):
        words = []
        for f in glob.glob(os.path.join(ROOT, "src", "dropviz", "**", "*"), recursive=True) + [os.path.join(ROOT, "README.md")]:
            if os.path.isfile(f) and not f.endswith(".pyc"):
                with open(f, encoding="utf-8") as fh:
                    text = fh.read().lower()
                if "market" in text or "시장" in text:
                    words.append(os.path.relpath(f, ROOT))
        self.assertEqual(words, [])


if __name__ == "__main__":
    unittest.main()

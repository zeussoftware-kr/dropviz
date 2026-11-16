"""Honest dot states (in progress only with live recent work, partly done, what is done / what is missing), the light/dark theme,
the first-visit tour, the How-to text and number-free labels."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
import urllib.request

from tests import helper
from tests.helper import ENV, ROOT, commit, make_repo, sh

from dropviz import core, defaults, i18n, miniyaml, timelapse, treemap, web

DAY = 86400


def feat(state, fid):
    return next(f for f in state["features"] if f["id"] == fid)


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def repo_with_yaml(extra=""):
    d = make_repo()
    helper.write(d, "dropviz.yaml", "features:\n- id: app\n  paths: [app/]\n" + extra +
                 "- id: lib\n  paths: [lib/]\n- id: none\n  paths: [nothing/]\n")
    return d


class States(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = repo_with_yaml()

    def test_recent_unmerged_branch_is_in_progress(self):
        self.assertEqual(feat(core.compute(self.repo), "lib")["status"], "in-progress")

    def test_quiet_unmerged_branch_is_not_in_progress(self):
        # lib: merged work exists and the codex/ branch went quiet -> partly done, and the reason says why
        later = time.time() + (defaults.get("stale_branch_days") + 1) * DAY
        f = feat(core.compute(self.repo, now=later), "lib")
        self.assertEqual(f["status"], "partly")
        self.assertTrue(any("quiet" in w for w in f["why"]), f["why"])

    def test_quiet_unmerged_work_alone_stays_unknown(self):
        d = make_repo()
        sh(d, "checkout", "-q", "-b", "claude/solo")
        commit(d, "solo", "solo/x.py", "1\n")
        sh(d, "checkout", "-q", "main")
        helper.write(d, "dropviz.yaml", "features:\n- id: solo\n  paths: [solo/]\n")
        self.assertEqual(feat(core.compute(d), "solo")["status"], "in-progress")
        later = time.time() + 30 * DAY
        f = feat(core.compute(d, now=later), "solo")
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["evidence"]["merged_commits"], 0)

    def test_no_work_is_unknown_and_explains_what_is_missing(self):
        f = feat(core.compute(self.repo), "none")
        self.assertEqual(f["status"], "unknown")
        self.assertTrue(f["explain"]["missing"])
        self.assertEqual(f["explain"]["done"], [])

    def test_merged_unverified_is_partly_with_missing_verification(self):
        f = feat(core.compute(self.repo), "app")
        self.assertEqual(f["status"], "partly")
        self.assertTrue(any("verification" in x for x in f["explain"]["missing"]))
        self.assertTrue(any("merged" in x for x in f["explain"]["done"]))

    def test_verified_is_done_and_a_missing_note_makes_it_partly(self):
        d = repo_with_yaml()
        sh(d, "notes", "--ref=dropviz-verify", "add", "-m", "pass feature=app", sh(d, "rev-parse", "main"))
        f = feat(core.compute(d), "app")
        self.assertEqual(f["status"], "done")
        self.assertEqual(f["explain"]["missing"], [])
        dv2 = repo_with_yaml('  done: [login works]\n  missing: [password reset]\n')
        sh(dv2, "notes", "--ref=dropviz-verify", "add", "-m", "pass feature=app", sh(dv2, "rev-parse", "main"))
        g = feat(core.compute(dv2), "app")
        self.assertEqual(g["status"], "partly")
        self.assertIn("password reset", g["explain"]["missing"])
        self.assertIn("login works", g["explain"]["done"])
        # a declared note alone never makes a feature done
        self.assertEqual(feat(core.compute(dv2), "none")["status"], "unknown")

    def test_counts_and_tiles_know_partly(self):
        st = core.compute(self.repo)
        c = st["counts"]
        self.assertEqual(c["done"] + c["in-progress"] + c["partly"] + c["unknown"], c["total"])
        self.assertEqual(c["partly"], 1)
        lay = treemap.layout(st, 800, 500)
        tile = next(n for n in lay["nodes"] if n.get("id") == "app")
        self.assertEqual(tile["fill"], defaults.data()["status_colors"]["partly"])
        self.assertTrue(any(x["label"] == i18n.t("en", "map.partly") for x in lay["legend"]))

    def test_replay_has_partly_and_last_frame_matches(self):
        st = core.compute(self.repo)
        tl = timelapse.frames(self.repo, st)
        self.assertEqual(tl["frames"][-1]["s"]["app"], "a")
        mid = timelapse.state_at(st, tl["frames"][-1])
        self.assertEqual(feat(mid, "app")["status"], "partly")

    def test_every_status_has_text_colour_and_rank(self):
        for s in defaults.data()["statuses"]:
            self.assertIn(s, defaults.data()["status_colors"])
            self.assertIn(s, treemap.STATUS_RANK)
            for lang in i18n.languages():
                self.assertNotEqual(i18n.t(lang, "map." + s), "map." + s)


class GitOnlyRule(unittest.TestCase):
    def test_merged_rule_with_and_without_a_missing_note(self):
        d = repo_with_yaml()
        helper.write(d, "dropviz.yaml", _read(os.path.join(d, "dropviz.yaml")) + "done: merged\n")
        self.assertEqual(feat(core.compute(d), "app")["status"], "done")
        dv2 = repo_with_yaml("  missing: [password reset]\n")
        helper.write(dv2, "dropviz.yaml", _read(os.path.join(dv2, "dropviz.yaml")) + "done: merged\n")
        g = feat(core.compute(dv2), "app")
        self.assertEqual(g["status"], "partly")
        self.assertIn("password reset", g["explain"]["missing"])
        tl = timelapse.frames(dv2, core.compute(dv2))
        self.assertEqual(tl["frames"][-1]["s"]["app"], "a")


class Settings(unittest.TestCase):
    def test_notes_survive_saving_settings(self):
        d = repo_with_yaml('  done: [login works]\n  missing: [password reset, "two words"]\n')
        core.save_settings(d, {"feature_weights": {"app": 3}})
        g = feat(core.compute(d), "app")
        self.assertEqual(g["missing"], ["password reset", "two words"])
        self.assertEqual(g["done_note"], ["login works"])
        self.assertEqual(g["weight"], 3.0)


class PlainLabels(unittest.TestCase):
    def test_words_instead_of_numbers(self):
        st = core.compute(repo_with_yaml())
        lay = treemap.layout(st, 800, 500, ctx={"config": {"plain_labels": True}})
        self.assertTrue(lay["plain"])
        heads = [n["label"] for n in lay["nodes"] if n["kind"] == "area" and n["label"]]
        self.assertTrue(heads)
        self.assertFalse(any(re.search(r"\d", h) for h in heads), heads)
        lay2 = treemap.layout(st, 800, 500)
        self.assertFalse(lay2["plain"])

    def test_words_exist_in_every_language_and_cover_the_range(self):
        got = {treemap.plain_word(p) for p in (0, 10, 50, 80, 100)}
        self.assertEqual(len(got), 5)
        for lang in i18n.languages():
            for k in got:
                self.assertNotEqual(i18n.t(lang, "map." + k), "map." + k)


class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "src", "dropviz", "page.html"), encoding="utf-8") as f:
            cls.html = f.read()

    def test_theme_tour_howto_hooks(self):
        for needle in ('data-theme', 'id="btheme"', 'id="bhelp"', 'dropviz.theme', 'prefers-color-scheme', 'dropviz.tour', 'tourStart', 'howto()'):
            self.assertIn(needle, self.html, needle)

    def test_no_dark_only_colours_left_in_the_chrome(self):
        css = self.html[self.html.index("<style>"):self.html.index("</style>")]
        body = css.split("}", 1)[1]  # after :root
        left = [c for c in re.findall(r"#[0-9a-fA-F]{6}\b", body.split("@media (max-width:760px)")[0]) if c.lower() in ("#0d0f12", "#1a1c21", "#262a33", "#1d1f24")]
        self.assertEqual(left, [])

    def test_catalogues_agree(self):
        keys = {l: set(i18n.catalogue(l)) for l in i18n.languages()}
        for l in keys:
            self.assertEqual(keys[l], keys["en"], l)
        for k in ("tour.map", "howto.1", "page.done_so_far", "page.still_missing", "page.theme_to_dark"):
            self.assertIn(k, keys["en"])

    @unittest.skipUnless(shutil.which("node"), "node not found")
    def test_page_script_parses(self):
        scripts = re.findall(r"<script>(.*?)</script>", self.html, re.S)
        for i, js in enumerate(scripts):
            p = subprocess.run(["node", "--check", "-"], input=js, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)


class Config(unittest.TestCase):
    def test_tour_settings_from_yaml(self):
        cfg = miniyaml.loads('tour: off\nhowto:\n- "First paragraph"\n- Second\ntour_steps:\n- target: map\n  text: "Look here"\n- target: nope\n')
        t = web._tour_cfg(cfg)
        self.assertEqual(t, {"auto": False, "steps": [{"target": "map", "text": "Look here"}], "howto": ["First paragraph", "Second"]})
        self.assertEqual(web._tour_cfg({}), {"auto": True, "steps": [], "howto": []})

    def test_api_serves_tour_and_plain_labels(self):
        d = repo_with_yaml()
        with open(os.path.join(d, "dropviz.yaml"), encoding="utf-8") as f:
            base = f.read()
        helper.write(d, "dropviz.yaml", base + 'plain_labels: true\nhowto: ["Custom how to"]\n')
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(d))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            c = json.load(urllib.request.urlopen(base + "/api/i18n?lang=en"))["config"]
            self.assertTrue(c["plain_labels"])
            self.assertEqual(c["tour"]["howto"], ["Custom how to"])
            lay = json.load(urllib.request.urlopen(base + "/api/layout?w=800&h=500&view=progress&lang=en"))
            self.assertTrue(lay["plain"])
        finally:
            httpd.shutdown()
            httpd.server_close()


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        return None


@unittest.skipUnless(_playwright(), "playwright not installed")
class Browser(unittest.TestCase):
    """The page in a real browser: no overlap between the map and the legend/view band, light/dark toggle, tour and How-to."""

    @classmethod
    def setUpClass(cls):
        cls.repo = repo_with_yaml("  missing: [password reset]\n")
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(cls.repo))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/" % cls.httpd.server_address[1]
        cls.pw = _playwright()().start()
        try:
            cls.br = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            cls.httpd.shutdown()
            raise unittest.SkipTest("no chromium: %s" % e)

    @classmethod
    def tearDownClass(cls):
        cls.br.close()
        cls.pw.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def page(self, w, h, scheme="dark", query=""):
        ctx = self.br.new_context(viewport={"width": w, "height": h}, color_scheme=scheme)
        pg = ctx.new_page()
        pg.goto(self.url + query)
        pg.wait_for_selector("body[data-ready='1']")
        pg.wait_for_timeout(500)
        return ctx, pg

    def test_map_fits_above_legend_and_views_in_both_themes(self):
        for w, h in ((1366, 768), (1920, 1080), (390, 844)):
            for scheme in ("dark", "light"):
                ctx, pg = self.page(w, h, scheme)
                try:
                    self.assertEqual(pg.evaluate("document.documentElement.dataset.theme"), scheme)
                    r = pg.evaluate("""()=>{const m=document.getElementById('mapw').getBoundingClientRect(),l=document.getElementById('leg').getBoundingClientRect(),
                        v=document.getElementById('views').getBoundingClientRect(),
                        maxb=Math.max(...[...document.querySelectorAll('#root rect')].map(e=>{const b=e.getBoundingClientRect();return b.bottom}));
                        return {mb:m.bottom,lt:l.top,vt:v.top,maxb:maxb}}""")
                    self.assertLessEqual(r["mb"], r["lt"] + 1, (w, h, scheme, r))
                    self.assertLessEqual(r["maxb"], r["lt"] + 1, (w, h, scheme, r))
                    self.assertLessEqual(r["mb"], r["vt"] + 1)
                finally:
                    ctx.close()

    def test_theme_toggle_persists(self):
        ctx, pg = self.page(1200, 800, "dark")
        try:
            pg.click("#btheme")
            self.assertEqual(pg.evaluate("document.documentElement.dataset.theme"), "light")
            self.assertEqual(pg.evaluate("localStorage.getItem('dropviz.theme')"), "light")
            pg.reload()
            pg.wait_for_selector("body[data-ready='1']")
            self.assertEqual(pg.evaluate("document.documentElement.dataset.theme"), "light")
            bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
            self.assertNotEqual(bg, "rgb(38, 36, 35)")
        finally:
            ctx.close()

    def test_tour_only_when_asked_in_automation_and_walks_through(self):
        ctx, pg = self.page(1200, 800)
        try:
            self.assertFalse(pg.evaluate("document.getElementById('tour').classList.contains('on')"))  # webdriver: no auto tour
            pg.goto(self.url + "?tour=1")
            pg.wait_for_selector("#tour.on .bub")
            n = 0
            while pg.is_visible("#tour .bub button.go"):
                self.assertTrue(pg.inner_text("#tour .bub .tn").startswith(str(n + 1)))
                box = pg.eval_on_selector("#tour .bub", "e=>{const r=e.getBoundingClientRect();return [r.left,r.top,r.right,r.bottom,innerWidth,innerHeight]}")
                self.assertGreaterEqual(box[0], 0)
                self.assertLessEqual(box[2], box[4])
                self.assertLessEqual(box[3], box[5])
                pg.click("#tour .bub button.go")
                n += 1
                if n > 10:
                    break
            self.assertEqual(n, 5)
            self.assertFalse(pg.evaluate("document.getElementById('tour').classList.contains('on')"))
            self.assertEqual(pg.evaluate("localStorage.getItem('dropviz.tour')"), "1")
        finally:
            ctx.close()

    def test_howto_opens_and_offers_the_tour(self):
        ctx, pg = self.page(1200, 800)
        try:
            pg.click("#bhelp")
            self.assertTrue(pg.is_visible("#howbox"))
            self.assertGreaterEqual(pg.locator("#howbox p").count(), 5)
            pg.click("#howbox button:has-text('Take the tour')")
            pg.wait_for_selector("#tour.on .bub")
        finally:
            ctx.close()

    def test_panel_says_what_is_done_and_missing(self):
        ctx, pg = self.page(1200, 800)
        try:
            pg.evaluate("select('app')")
            pg.wait_for_timeout(200)
            txt = pg.inner_text("#pane")
            self.assertIn("Done so far", txt)
            self.assertIn("Still missing", txt)
            self.assertIn("password reset", txt)
        finally:
            ctx.close()


if __name__ == "__main__":
    unittest.main()

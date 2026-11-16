#!/usr/bin/env python3
"""Regenerate docs/screenshots/*.png and docs/timelapse.gif from the invented example repo (no real data involved).
Needs Playwright (pip install playwright; playwright install chromium) and, for the GIF, ffmpeg.
Usage: python3 examples/make_screenshots.py"""
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))


def main():
    from playwright.sync_api import sync_playwright
    tmp = tempfile.mkdtemp(prefix="dropviz-shots-")
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "make_demo_repo.py"), os.path.join(tmp, "kestrel")], check=True, capture_output=True)
        sd = os.path.join(tmp, "kestrel-sessions")
        os.environ.update(DROPVIZ_LANG="en", DROPVIZ_MACHINE_NAME="dev-laptop", DROPVIZ_CACHE_DIR=os.path.join(tmp, "cache"), DROPVIZ_CLAUDE_DIR=sd + "/claude",
                          DROPVIZ_CODEX_DIR=sd + "/codex", DROPVIZ_OPENCODE_DB=sd + "/opencode.db")
        from dropviz import core, mcp, render, tokens, web
        root = os.path.join(tmp, "kestrel")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(root))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d/" % httpd.server_address[1]
        out = os.path.join(ROOT, "docs", "screenshots")
        os.makedirs(out, exist_ok=True)
        st = core.compute(root)
        t = tokens.tokens(root)
        fm = tokens.feature_models(root, t, st["features"])
        pick = next(f for f in st["features"] if f["status"] == "done" and len(fm.get(f["id"], {})) >= 1 and any(c["kind"] == "ai" for c in f["evidence"]["commits"]))
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1440, "height": 860}, color_scheme="dark")
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(url)
            pg.wait_for_selector("body[data-ready]", timeout=60000)
            pg.wait_for_function("S.tokens && S.metrics", timeout=60000)
            pg.wait_for_timeout(800)
            n_tiles = pg.evaluate("document.querySelectorAll('#root g.tile').length")
            assert n_tiles == len(st["features"]) + st["counts"]["planned"], (n_tiles, len(st["features"]))
            pg.screenshot(path=out + "/map.png")
            pg.evaluate("zoom('Library')")
            pg.wait_for_timeout(900)
            pg.screenshot(path=out + "/zoom.png")
            pg.evaluate("zoom('')")
            pg.wait_for_timeout(700)
            pg.evaluate("select(%s)" % json.dumps(pick["id"]))
            pg.wait_for_timeout(500)
            pg.screenshot(path=out + "/feature.png")
            pg.evaluate("S.view='risk';paintStatic();refresh(false)")
            pg.wait_for_timeout(700)
            pg.screenshot(path=out + "/risk.png")
            pg.evaluate("S.view='progress';S.tab='now';paintStatic();paintPane();refresh(false)")
            pg.wait_for_timeout(700)
            pg.screenshot(path=out + "/working.png")
            pg.evaluate("S.tab='models';paintStatic();paintPane()")
            pg.wait_for_timeout(400)
            pg.screenshot(path=out + "/models.png")
            pg.evaluate("openSettings()")
            pg.wait_for_timeout(700)
            pg.screenshot(path=out + "/settings.png")
            pg.evaluate("document.getElementById('mset').classList.remove('on');S.tab='detail';select(%s);setTheme('light',false);paintStatic();paintPane();refresh(false)" % json.dumps(pick["id"]))
            pg.wait_for_timeout(700)
            pg.screenshot(path=out + "/light.png")
            pg.close()
            ph = b.new_page(viewport={"width": 390, "height": 800}, device_scale_factor=2, color_scheme="dark")
            ph.goto(url)
            ph.wait_for_selector("body[data-ready]", timeout=60000)
            ph.wait_for_timeout(1200)
            ph.screenshot(path=out + "/phone.png")
            # the MCP answer, as a terminal-like box (a real status call, trimmed)
            srv = mcp.Server(root)
            ans = srv.call("status", {})
            ans.pop("areas")
            ans.pop("models_tokens")
            ans["working_now"] = ans["working_now"][:2]
            ans["features"] = ans["features"][:2] + [{"...": "%d more" % (len(ans["features"]) - 2)}]
            ans["model_ranking"]["rows"] = ans["model_ranking"]["rows"][:3]
            ans = {k: ans[k] for k in ("repo", "counts", "rule", "working_now", "model_ranking", "tokens_total", "features")}
            text = json.dumps(ans, indent=1, ensure_ascii=False)
            term = ("<html><body style='margin:0;background:#0f1218'><div style='margin:18px;border:1px solid #2c303a;border-radius:10px;background:#14161a;"
                    "width:1000px;box-shadow:0 8px 30px #000a'><div style='padding:8px 12px;background:#1d2027;color:#9aa1ad;font:12px sans-serif'>"
                    "claude code - dropviz MCP: status</div><pre style='margin:0;padding:14px;color:#d6dae2;font:12.5px/1.45 ui-monospace,Menlo,monospace;"
                    "white-space:pre-wrap'>%s</pre></div></body></html>" % html.escape(text[:4200]))
            tp = b.new_page(viewport={"width": 1040, "height": 700})
            tp.set_content(term)
            tp.screenshot(path=out + "/mcp.png")
            b.close()
        if errors:
            sys.exit("page errors: %s" % errors)
        gif = os.path.join(ROOT, "docs", "timelapse.gif")
        if shutil.which("ffmpeg"):
            render.timelapse(root, gif, "progress", "800x450", 5)
            print("wrote", gif, os.path.getsize(gif), "bytes")
        httpd.shutdown()
        for f in sorted(os.listdir(out)):
            print("wrote", os.path.join(out, f))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

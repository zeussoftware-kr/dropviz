"""Headless rendering of the map page to files (screenshots, timelapse GIF/MP4). Local only; nothing is uploaded.
Uses Playwright's Chromium when the python package is installed, else a Chrome/Chromium binary. ffmpeg (or Pillow)
joins the frames; without either, the PNG frames are kept in a folder next to the output."""
import math
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer

from . import core, defaults, i18n, web
from . import timelapse as history


class RenderError(Exception):
    pass


def _server(root):
    httpd = ThreadingHTTPServer((defaults.get("host"), 0), web.make_handler(root))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://%s:%d/" % (defaults.get("host"), httpd.server_address[1])


def _size(text):
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise RenderError(text)


def chrome():
    return next((c for c in defaults.get("chrome_candidates") if shutil.which(c) or os.path.exists(c)), None)


def shoot_all(url, jobs, size, tmp):
    """jobs: [(hash fragment, out png)]. One browser, one page (Playwright) or one Chrome run per job."""
    w, h = _size(size)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sync_playwright = None
    if sync_playwright:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": w, "height": h})
            pg.goto(url)
            pg.wait_for_selector("body[data-ready]", timeout=defaults.get("render_timeout_seconds") * 1000)
            for frag, out in jobs:
                pg.evaluate("f => { const p = new URLSearchParams(f); return (async () => { if (p.get('tl') !== null) await window.__setFrame(+p.get('tl')); })() }", frag)
                pg.wait_for_timeout(120)
                pg.screenshot(path=out)
            b.close()
        return
    exe = chrome()
    if not exe:
        raise RenderError(i18n.t(i18n.from_env(), "err.no_browser"))
    for frag, out in jobs:
        subprocess.run([exe] + defaults.get("chrome_flags") + ["--user-data-dir=" + os.path.join(tmp, "profile"), "--virtual-time-budget=8000",
                                                           "--window-size=%d,%d" % (w, h), "--screenshot=" + out, url + "#" + frag],
                       check=True, capture_output=True, env=dict(os.environ, HOME=tmp))


def timelapse_frames(root, out, view, size, fps=None):
    fps = fps or defaults.get("render_fps")
    tl = history.frames(root)
    n = len(tl["frames"])
    if not n:
        raise RenderError(i18n.t(i18n.from_env(), "err.no_history"))
    step = max(1, math.ceil(n / float(fps * defaults.get("render_max_seconds"))))
    idx = sorted(set(list(range(0, n, step)) + [n - 1]))
    return tl, idx, fps


def timelapse(root, out, view, size, fps=None):
    out = os.path.abspath(out)
    ext = os.path.splitext(out)[1].lower()
    httpd, url = _server(root)
    tmp = tempfile.mkdtemp(prefix="dropviz-render-")
    try:
        if ext == ".png":
            shoot_all(url, [("v=" + view, out)], size, tmp)
            return out
        tl, idx, fps = timelapse_frames(root, out, view, size, fps)
        jobs = [("v=%s&tl=%d" % (view, i), os.path.join(tmp, "f%04d.png" % n)) for n, i in enumerate(idx)]
        shoot_all(url, jobs, size, tmp)
        ff = shutil.which("ffmpeg")
        if ff and ext in (".mp4", ".gif"):
            pat = os.path.join(tmp, "f%04d.png")
            if ext == ".mp4":
                cmd = [ff, "-y", "-loglevel", "error", "-framerate", str(fps), "-i", pat, "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
            else:
                cmd = [ff, "-y", "-loglevel", "error", "-framerate", str(fps), "-i", pat, "-vf",
                       "split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=none", out]
            subprocess.run(cmd, check=True)
            return out
        try:
            from PIL import Image
            ims = [Image.open(os.path.join(tmp, "f%04d.png" % n)).convert("P", palette=Image.ADAPTIVE, colors=96) for n in range(len(idx))]
            ims[0].save(os.path.splitext(out)[0] + ".gif", save_all=True, append_images=ims[1:], duration=int(1000 / fps), loop=0, optimize=True)
            return os.path.splitext(out)[0] + ".gif"
        except ImportError:
            keep = os.path.splitext(out)[0] + "-frames"
            shutil.copytree(tmp, keep, ignore=shutil.ignore_patterns("profile"))
            return keep
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

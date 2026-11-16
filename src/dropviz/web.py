"""Local map page and its JSON API. Binds 127.0.0.1 only; nothing is fetched from or sent to the network."""
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import core, defaults, i18n, live, metrics, timelapse, tokens, treemap, trailers

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 1 << 20
TTL = {"state": 4, "metrics": 15, "tokens": 30, "timelapse": 600, "live": 3, "trailers": 60}


def page():
    with open(os.path.join(HERE, "page.html"), encoding="utf-8") as f:
        return f.read()


class Cache:
    def __init__(self, root):
        self.root, self.data = root, {}

    def get(self, key, fn):
        hit = self.data.get(key)
        if hit and time.time() - hit[0] < TTL[key]:
            return hit[1]
        val = fn()
        self.data[key] = (time.time(), val)
        return val

    def clear(self):
        self.data.clear()

    def state(self):
        return self.get("state", lambda: core.compute(self.root))

    def metrics(self, lang=None):
        return self.get("metrics", lambda: metrics.compute(self.root, lang=lang))

    def tokens(self):
        def run():
            st = self.state()
            t = tokens.tokens(self.root)
            fm = tokens.feature_models(self.root, t, st["features"])
            ft = tokens.feature_tokens(self.root, t, st["features"])
            by_id = {f["id"]: f for f in st["features"]}
            touched, done = {}, {}
            for fid, ms in fm.items():
                for m in ms:
                    touched[m] = touched.get(m, 0) + 1
                    done[m] = done.get(m, 0) + (by_id[fid]["status"] == "done")
            t["features"] = ft
            t["summary"] = tokens.summary(t)
            t["ranking"] = tokens.ranking(st["features"], t, fm)
            t["models"] = {"by_model": t["by_model"], "features": fm, "touched": touched, "done": done}
            return t
        return self.get("tokens", run)

    def trailers(self):
        """Models named in commit trailers (all time). Rows are kept so the replay can cut them at a day."""
        return self.get("trailers", lambda: trailers.summarize(self.root, self.state()))

    def live(self):
        return self.get("live", lambda: live.working_now(self.root, self.state()))

    def timelapse(self):
        return self.get("timelapse", lambda: timelapse.frames(self.root, self.state()))


def trailer_ctx(cache, st, until=None):
    tr = cache.trailers()
    if until is not None:
        tr = trailers.summarize(cache.root, st, until, rows=tr["rows"], events=tr["events"])
    return dict(tr, colors=trailers.colors(tr))


def layout_ctx(cache, want_models=False, view="", until=None):
    st = cache.state()
    ctx = {"now": time.time(), "attention": core.attention(cache.root), "config": core.load_config(cache.root)}
    ctx["metrics"] = None
    if view == "speed":  # only this view needs the per-feature weekly counts
        try:
            ctx["metrics"] = cache.metrics()
        except Exception:
            pass
    ctx["done_rule"] = st.get("done_rule", "strict")
    if view == "models" or want_models:
        try:
            ctx["trailers"] = trailer_ctx(cache, st, until)
        except Exception:
            pass
    if want_models or st.get("work_unit") == "tokens":
        try:
            t = cache.tokens()
            ctx["models"] = t["models"]["features"]
            ctx["tokens"] = {fid: sum(c["input"] + c["cache_read"] + c["cache_write"] + c["output"] for c in ms.values())
                             for fid, ms in t["models"]["features"].items()}
        except Exception:
            pass
    try:
        ctx["live_by_feature"] = cache.live()["by_feature"]
    except Exception:
        ctx["live_by_feature"] = {}
    return ctx


def _tree_out(nodes):
    return [{"name": n["name"], "weight": n.get("weight"), "planned": n.get("planned"), "areas": _tree_out(n["children"])}
            for n in nodes]


def make_handler(root):
    cache = Cache(root)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in LOCAL_HOSTS  # DNS-rebinding guard

        def _send(self, body, ctype, status=200):
            if not isinstance(body, bytes):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status=200):
            self._send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", status)

        def do_GET(self):
            if not self._host_ok():
                self.send_error(403)
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            lang = i18n.pick(q.get("lang"), self.headers.get("Accept-Language"), os.environ.get("DROPVIZ_LANG"))
            try:
                self._route(u.path, q, lang)
            except Exception as e:  # a reader problem must not take the page down
                self._json({"error": str(e)}, 500)

        def _route(self, path, q, lang):
            cfg = core.load_config(root)
            if path == "/":
                self._send(page(), "text/html; charset=utf-8")
            elif path == "/api/i18n":
                self._json({"lang": lang, "catalogue": i18n.catalogue(lang), "languages": i18n.languages(),
                            "config": {"views": list(treemap.VIEWS), "refresh": defaults.get("refresh_seconds", cfg),
                                       "fps": defaults.get("timelapse_fps", cfg),
                                       "plain_labels": bool(defaults.get("plain_labels", cfg)), "tour": _tour_cfg(cfg)}})
            elif path == "/api/state":
                self._json(cache.state())
            elif path == "/api/metrics":
                self._json(cache.metrics(lang))
            elif path == "/api/tokens":
                self._json(cache.tokens())
            elif path == "/api/trailers":
                tr = cache.trailers()
                self._json({k: tr[k] for k in ("by_model", "features", "areas", "commits_total", "commits_with_model", "unit")})
            elif path == "/api/live":
                self._json(cache.live())
            elif path == "/api/timelapse":
                tl = cache.timelapse()
                self._json({"days": tl["days"], "counts": tl["counts"]})
            elif path == "/api/settings":
                st = cache.state()
                self._json({"areas": _tree_out(st["areas"]), "size_by": st["size_by"], "work_unit": st["work_unit"],
                            "rules": defaults.get("size_rules", cfg), "units": defaults.get("work_units", cfg)})
            elif path == "/api/layout":
                st = cache.state()
                at, until = q.get("at"), None
                if at not in (None, ""):
                    tl = cache.timelapse()
                    fr = tl["frames"][max(0, min(int(at), len(tl["frames"]) - 1))]
                    st = timelapse.state_at(st, fr)
                    until = timelapse._day_end(fr["day"])
                ctx = layout_ctx(cache, want_models=bool(q.get("model")), view=q.get("view", ""), until=until)
                self._json(treemap.layout(st, int(q.get("w", 1000)), int(q.get("h", 600)), q.get("view", "progress"),
                                          q.get("focus", ""), ctx, lang, model=q.get("model", "")))
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Dropviz-Local") != "1":
                self.send_error(403)
                return
            origin = self.headers.get("Origin")
            if origin and (urlparse(origin).hostname or "") not in LOCAL_HOSTS:
                self.send_error(403)
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.send_error(413)
                return
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._json({"error": i18n.t(i18n.from_env(), "err.bad_json")}, 400)
                return
            path = urlparse(self.path).path
            try:
                if path == "/api/settings":
                    res = core.save_settings(root, body)
                    cache.clear()
                    self._json(res)
                elif path == "/api/attention":
                    core.bump_attention(root, str(body.get("feature", "")), "opens")
                    self._json({"ok": True})
                else:
                    self.send_error(404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
    return H


def _tour_cfg(cfg):
    """The first-visit tour and the How-to text from dropviz.yaml: `tour: off`, `tour_steps:` (list of target/text) and `howto:` (list of
    paragraphs). Anything missing falls back to the built-in text."""
    steps = [{"target": str(x.get("target") or "map"), "text": x.get("text")} for x in (cfg.get("tour_steps") or [])
             if isinstance(x, dict) and x.get("text")]
    how = cfg.get("howto") or []
    how = [str(x) for x in (how if isinstance(how, list) else [how]) if str(x).strip()]
    return {"auto": cfg.get("tour") not in (False, "off", "false", "no"), "steps": steps, "howto": how}


def serve(root, port=None, open_browser=False):
    port = defaults.get("port") if port is None else port
    httpd = ThreadingHTTPServer((defaults.get("host"), port), make_handler(root))
    url = "http://%s:%d/" % (defaults.get("host"), httpd.server_address[1])
    print(i18n.t(i18n.from_env(), "cli.serving", url=url), flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

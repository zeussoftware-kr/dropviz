"""Minimal stdio MCP server (JSON-RPC 2.0, one JSON message per line), standard library only."""
import json
import os
import sys
import uuid

from . import __version__, core, defaults, i18n, live, tokens as tokens_mod

SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")

SCHEMAS = {
    "status": {"type": "object", "properties": {}},
    "next": {"type": "object", "properties": {"session": {"type": "string"}}},
    "claim": {"type": "object", "properties": {"feature": {"type": "string"}, "session": {"type": "string"},
                                                "note": {"type": "string"}}, "required": ["feature"]},
    "report": {"type": "object", "properties": {"feature": {"type": "string"}, "session": {"type": "string"},
                                                 "note": {"type": "string"}}, "required": ["feature"]},
    "tokens": {"type": "object", "properties": {"commit": {"type": "string"}, "branch": {"type": "string"},
                                                 "since": {"type": "string"}, "limit": {"type": "integer"}}},
    "metrics": {"type": "object", "properties": {}},
    "explain": {"type": "object", "properties": {"feature": {"type": "string"}}, "required": ["feature"]},
    "configure": {"type": "object", "properties": {
        "areas": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "was": {"type": "string"}, "weight": {"type": "number"}, "planned": {"type": "integer"},
            "areas": {"type": "array"}}, "required": ["name"]}},
        "size_by": {"type": "string"}, "work_unit": {"type": "string"},
        "feature_areas": {"type": "object"}, "feature_weights": {"type": "object"}}},
}


def tools(lang):
    return [{"name": n, "description": i18n.t(lang, "mcp.tool_" + n), "inputSchema": SCHEMAS[n]} for n in SCHEMAS]


TOOLS = tools("en")  # tool names are fixed; descriptions follow the language of tools/list


class Server:
    def __init__(self, root):
        self.root = root
        self.session = "mcp-%d-%s" % (os.getpid(), uuid.uuid4().hex[:6])
        self.lang = i18n.from_env()

    def _feature(self, state, fid):
        for f in state["features"]:
            if f["id"] == fid:
                return f
        raise ValueError(i18n.t(self.lang, "mcp.unknown_feature", id=fid, known=", ".join(f["id"] for f in state["features"])))

    def call(self, name, a):
        a = a or {}
        sess = a.get("session") or self.session
        if name == "tokens":
            from . import tokens
            res = tokens.tokens(self.root, sources=getattr(self, "sources", None), since=a.get("since"),
                                commit=a.get("commit"), branch=a.get("branch"))
            order = sorted(res["commits"], key=lambda s: -sum(c["input"] + c["output"] for c in res["commits"][s].values()))
            lim = int(a.get("limit") or 50)
            res["commits_omitted"] = max(0, len(order) - lim)
            res["models"] = res.get("by_model", {})
            res["commits"] = {s: res["commits"][s] for s in order[:lim]}
            return res
        if name == "metrics":
            from . import metrics
            return metrics.compute(self.root)
        state = core.compute(self.root)
        if name == "status":
            try:
                lv = live.working_now(self.root, state)
                working = [{k: e.get(k) for k in ("tool", "model", "branch", "features", "minutes", "last", "stalled", "kind")}
                           for e in lv["entries"]]
            except Exception:
                working = []
            try:
                tk = tokens_mod.tokens(self.root)
                models = {m: sum(c[k] for k in tokens_mod.FIELDS) for m, c in tk["by_model"].items()}
                fm = tokens_mod.feature_models(self.root, tk, state["features"])
                ranking, total = tokens_mod.ranking(state["features"], tk, fm), tokens_mod.summary(tk)["total"]
            except Exception:
                models, ranking, total = {}, None, None
            return {"repo": state["name"], "default_branch": state["default_branch"], "counts": state["counts"],
                    "rule": i18n.t(self.lang, "mcp.rule"), "areas": core.area_tree_out(state["areas"]),
                    "working_now": working, "models_tokens": models, "model_ranking": ranking, "tokens_total": total,
                    "features": [{k: f[k] for k in ("id", "name", "area", "status", "why", "claim", "reported_done")}
                                 for f in state["features"]]}
        if name == "next":
            rank = {"in-progress": 0, "unknown": 1}
            cands = []
            for f in state["features"]:
                if f["status"] == "done":
                    continue
                if f["claim"] and f["claim"]["session"] != sess:
                    continue
                cands.append((rank[f["status"]], 0 if f["paths"] else 1, f["id"], f))
            cands.sort(key=lambda c: c[:3])
            if not cands:
                return {"next": None, "message": i18n.t(self.lang, "mcp.nothing_next")}
            f = cands[0][3]
            return {"next": {k: f[k] for k in ("id", "name", "status", "why", "paths", "accept")},
                    "alternatives": [c[2] for c in cands[1:4]],
                    "hint": i18n.t(self.lang, "mcp.hint_claim")}
        if name == "claim":
            self._feature(state, a["feature"])
            ok, cur = core.claim(self.root, a["feature"], sess, a.get("note", ""))
            if not ok:
                raise RuntimeError(i18n.t(self.lang, "mcp.claimed_by", feature=a["feature"], session=cur["session"],
                                          secs=int(core.time.time() - cur["last"])))
            core.bump_attention(self.root, a["feature"], "mcp")
            return {"claimed": a["feature"], "session": sess, "expires_after_idle_seconds": core.CLAIM_TTL}
        if name == "report":
            self._feature(state, a["feature"])
            r = core.report(self.root, a["feature"], sess, a.get("note", ""))
            core.bump_attention(self.root, a["feature"], "mcp")
            return {"recorded": r, "status_unchanged": True, "message": i18n.t(self.lang, "mcp.report_note")}
        if name == "explain":
            core.bump_attention(self.root, a["feature"], "mcp")
            return self._feature(state, a["feature"])
        if name == "configure":
            if not a:
                return {"areas": core.area_tree_out(state["areas"]), "size_by": state["size_by"], "work_unit": state["work_unit"],
                        "message": i18n.t(self.lang, "mcp.configure_help")}
            res = core.save_settings(self.root, a)
            res["message"] = i18n.t(self.lang, "mcp.configured")
            return res
        raise KeyError(name)


def _resp(i, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": i}
    if error:
        m["error"] = error
    else:
        m["result"] = result
    return m


def handle(srv, msg):
    """Return a response dict, or None for notifications."""
    if not isinstance(msg, dict) or "method" not in msg:
        return None
    method, i = msg["method"], msg.get("id")
    if i is None:  # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        return _resp(i, {"protocolVersion": want if want in SUPPORTED else SUPPORTED[0],
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "dropviz", "version": __version__}})
    if method == "ping":
        return _resp(i, {})
    if method == "tools/list":
        return _resp(i, {"tools": tools(srv.lang)})
    if method == "tools/call":
        p = msg.get("params") or {}
        if p.get("name") not in SCHEMAS:
            return _resp(i, error={"code": -32602, "message": i18n.t(srv.lang, "mcp.unknown_tool", name=p.get("name"))})
        try:
            out, err = srv.call(p["name"], p.get("arguments")), False
        except Exception as e:  # tool errors are results with isError, per MCP
            out, err = str(e) or e.__class__.__name__, True
        text = out if isinstance(out, str) else json.dumps(out, indent=1, ensure_ascii=False)
        return _resp(i, {"content": [{"type": "text", "text": text}], "isError": err})
    return _resp(i, error={"code": -32601, "message": i18n.t(srv.lang, "mcp.no_method", method=method)})


def serve(root, stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    srv = Server(root)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = _resp(None, error={"code": -32700, "message": i18n.t(srv.lang, "mcp.parse_error")})
        else:
            out = handle(srv, msg)
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()

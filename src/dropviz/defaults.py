"""Every default value of dropviz lives in defaults.json. Code asks here; nothing is typed into the code.
Order of precedence: dropviz.yaml top-level key > environment DROPVIZ_<NAME> > defaults.json."""
import json
import os

_D = None


def data():
    global _D
    if _D is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults.json"), encoding="utf-8") as f:
            _D = json.load(f)
    return _D


def _coerce(raw, like):
    if isinstance(like, bool):
        return raw.lower() in ("1", "true", "yes")
    if isinstance(like, int):
        return int(raw)
    if isinstance(like, float):
        return float(raw)
    if isinstance(like, list):
        return [x for x in raw.split(",") if x]
    return raw


def get(name, cfg=None):
    """A top-level value or one entry of `thresholds`, with yaml and env overrides."""
    d = data()
    base = d["thresholds"][name] if name in d["thresholds"] else d[name]
    if cfg and cfg.get(name) not in (None, "", []):
        return cfg[name]
    env = os.environ.get("DROPVIZ_" + name.upper())
    if env:
        try:
            return _coerce(env, base)
        except ValueError:
            pass
    return base


def source(name):
    """A path from `sources`, ~ expanded; env DROPVIZ_<NAME> wins."""
    env = os.environ.get("DROPVIZ_" + name.upper())
    return os.path.expanduser(env or data()["sources"][name])

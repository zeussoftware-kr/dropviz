"""Message catalogue: every user-facing string is a key in i18n/<lang>.json. Add a language = add a file."""
import json
import os

from . import defaults

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")
_CACHE = {}
FALLBACK = "en"


def languages():
    return sorted(f[:-5] for f in os.listdir(_DIR) if f.endswith(".json"))


def catalogue(lang):
    if lang not in _CACHE:
        try:
            with open(os.path.join(_DIR, lang + ".json"), encoding="utf-8") as f:
                _CACHE[lang] = json.load(f)
        except (OSError, ValueError):
            _CACHE[lang] = {}
    return _CACHE[lang]


def pick(*candidates):
    """First candidate (a language tag like 'ko_KR.UTF-8' or 'ko-KR,en;q=0.8') that we ship; else the fallback."""
    have = set(languages())
    for c in candidates:
        for part in str(c or "").replace(";", ",").split(","):
            code = part.strip().split("_")[0].split("-")[0].split(".")[0].lower()
            if code in have:
                return code
    return FALLBACK


def from_env(setting=None):
    """CLI language: explicit setting, DROPVIZ_LANG, then LC_ALL / LC_MESSAGES / LANG."""
    e = os.environ
    return pick(setting, e.get("DROPVIZ_LANG"), e.get("LC_ALL"), e.get("LC_MESSAGES"), e.get("LANG"))


def t(lang, key, **kw):
    s = catalogue(lang).get(key)
    if s is None:
        s = catalogue(FALLBACK).get(key, key)
    try:
        return s.format(**kw) if kw else s
    except (KeyError, IndexError, ValueError):
        return s

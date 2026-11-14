"""A tiny reader/writer for the small YAML subset dropviz.yaml uses (no third-party YAML).

Supported: `key: value`, `key: [a, b]`, `key:` followed by `- item` lines, and list items that
are `- key: value` blocks with further indented `key: value` lines. Strings are written as JSON
strings (valid YAML double-quoted scalars)."""
import json
import re


def _scalar(s):
    s = s.strip()
    if not s:
        return ""
    if s[0] == '"':
        try:
            return json.JSONDecoder().raw_decode(s)[0]
        except ValueError:
            return s.strip('"')
    if s[0] == "'" and s[-1] == "'" and len(s) > 1:
        return s[1:-1]
    if s[0] == "[":
        end = s.rfind("]")
        inner = s[1:end] if end > 0 else s[1:]
        return [_scalar(p) for p in _split_flow(inner)]
    s = re.split(r"\s+#", s, 1)[0].strip()
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _split_flow(inner):
    parts, cur, q = [], "", False
    for ch in inner:
        if ch == '"':
            q = not q
        if ch == "," and not q:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p for p in parts if p.strip()]


def loads(text):
    data, key, item = {}, None, None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0 and not line.startswith("- "):
            k, _, v = line.partition(":")
            key, item = k.strip(), None
            data[key] = _scalar(v) if v.strip() else None
            continue
        if key is None:
            continue
        if line.startswith("- "):
            if not isinstance(data[key], list):
                data[key] = []
            body = line[2:].strip()
            if re.match(r'^[\w-]+:(\s|$)', body):
                k, _, v = body.partition(":")
                item = {k.strip(): _scalar(v)}
                data[key].append(item)
            else:
                item = None
                data[key].append(_scalar(body))
        elif item is not None and ":" in line:
            k, _, v = line.partition(":")
            item[k.strip()] = _scalar(v)
    return data


def _s(v):
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False)


HEAD_KEYS = ("name", "default_branch", "ai_branch_prefixes", "size_by", "work_unit", "worktree_roots")
ITEM_ORDER = ("id", "name", "area", "parent", "weight", "planned", "paths", "target", "accept", "status")


def _items(out, key, items):
    out.append("%s:" % key)
    for f in items:
        keys = [k for k in ITEM_ORDER if k in f] + [k for k in f if k not in ITEM_ORDER]
        for n, k in enumerate(keys):
            line = "%s%s: %s" % ("  - " if n == 0 else "    ", k, _s(f[k]))
            if k == "status":
                line += "  # ignored: computed from evidence"
            out.append(line)


def dumps_config(cfg):
    """Write the whole config back (comments are not kept; callers keep a backup of the old file)."""
    out = ["# dropviz.yaml - what this project is made of. Status is NEVER set here:",
           "# it is computed from git evidence (merged to the default branch + a verification record)."]
    for k in HEAD_KEYS:
        if k in cfg and cfg[k] not in (None, "", []):
            out.append("%s: %s" % (k, _s(cfg[k])))
    for k in cfg:
        if k not in HEAD_KEYS and k not in ("areas", "features") and isinstance(cfg[k], (str, int, float, bool)):
            out.append("%s: %s" % (k, _s(cfg[k])))
    for k in ("areas", "features"):
        if cfg.get(k):
            _items(out, k, cfg[k])
    return "\n".join(out) + "\n"

"""Register dropviz as an MCP server in the AI coding tools found on this computer, using each tool's own
official way: Claude Code via its `claude mcp` command, Codex via ~/.codex/config.toml, Gemini CLI via
~/.gemini/settings.json, OpenCode via its opencode.json. A config file is copied to a backup before it is
edited; running it twice changes nothing; `remove` undoes it. Returns a list of plain result records."""
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import defaults


def home():
    return os.path.expanduser(os.environ.get("DROPVIZ_HOME") or "~")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def cfg():
    return defaults.data()["connect"]


def server_command():
    """The command that starts dropviz's MCP server: `dropviz` if it is on PATH, else this python."""
    c = cfg()
    exe = shutil.which("dropviz")
    return ([exe] if exe else [sys.executable, "-m", "dropviz"]) + list(c["server_args"])


def _backup(path):
    if not os.path.isfile(path):
        return None
    dest = path + cfg()["backup_suffix"] + time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = "%s%s%s-%d" % (path, cfg()["backup_suffix"], time.strftime("%Y%m%d-%H%M%S"), n)
    shutil.copyfile(path, dest)
    return dest


def _rec(tool, state, **kw):
    return dict(kw, tool=tool, state=state)


# ----- Claude Code: the official CLI -----
def _claude(remove):
    c = cfg()
    exe = shutil.which(c["claude_cli"])
    if not exe:
        return None
    name = c["server_name"]
    have = subprocess.run([exe, "mcp", "get", name], capture_output=True, text=True).returncode == 0
    if remove:
        if not have:
            return _rec("claude", "absent")
        p = subprocess.run([exe, "mcp", "remove", name], capture_output=True, text=True)
        return _rec("claude", "removed" if p.returncode == 0 else "failed", detail=p.stderr.strip())
    if have:
        return _rec("claude", "already")
    p = subprocess.run([exe, "mcp", "add", name, "--"] + server_command(), capture_output=True, text=True)
    return _rec("claude", "added" if p.returncode == 0 else "failed", detail=p.stderr.strip())


# ----- Codex: ~/.codex/config.toml -----
def _toml_block(name):
    cmd = server_command()
    return '[mcp_servers.%s]\ncommand = %s\nargs = %s\n' % (name, json.dumps(cmd[0]), json.dumps(cmd[1:]))


def _codex(remove):
    c = cfg()
    if not os.path.isdir(os.path.join(home(), c["codex_dir"])):
        return None
    path = os.path.join(home(), c["codex_config"])
    text = _read(path) if os.path.isfile(path) else ""
    head = re.compile(r"^\[mcp_servers\.%s\]\s*$" % re.escape(c["server_name"]), re.M)
    m = head.search(text)
    if remove:
        if not m:
            return _rec("codex", "absent", path=path)
        end = re.compile(r"^\[", re.M).search(text, m.end())
        new = text[:m.start()] + (text[end.start():] if end else "")
        b = _backup(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new.rstrip("\n") + "\n" if new.strip() else "")
        return _rec("codex", "removed", path=path, backup=b)
    if m:
        return _rec("codex", "already", path=path)
    b = _backup(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + ("\n" if text and not text.endswith("\n\n") else "") + _toml_block(c["server_name"]))
    return _rec("codex", "added", path=path, backup=b)


# ----- JSON configs (Gemini CLI, OpenCode) -----
def _json_tool(tool, path, dirname, key, entry, remove):
    c = cfg()
    if not os.path.isdir(os.path.join(home(), dirname)):
        return None
    data = {}
    if os.path.isfile(path):
        raw = _read(path)
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:  # comments or a format we will not guess at: leave the file alone
            return _rec(tool, "manual", path=path, line=json.dumps({key: {c["server_name"]: entry}}))
    group = data.get(key) if isinstance(data.get(key), dict) else None
    has = bool(group and c["server_name"] in group)
    if remove:
        if not has:
            return _rec(tool, "absent", path=path)
        b = _backup(path)
        del group[c["server_name"]]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return _rec(tool, "removed", path=path, backup=b)
    if has:
        return _rec(tool, "already", path=path)
    b = _backup(path)
    data.setdefault(key, {})[c["server_name"]] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return _rec(tool, "added", path=path, backup=b)


def _gemini(remove):
    c = cfg()
    cmd = server_command()
    return _json_tool("gemini", os.path.join(home(), c["gemini_config"]), c["gemini_dir"], "mcpServers",
                      {"command": cmd[0], "args": cmd[1:]}, remove)


def _opencode(remove):
    c = cfg()
    paths = [os.path.join(home(), p) for p in c["opencode_configs"]]
    path = next((p for p in paths if os.path.isfile(p)), paths[0])
    return _json_tool("opencode", path, c["opencode_dir"], "mcp", {"type": "local", "command": server_command(), "enabled": True}, remove)


def run(remove=False, only=None):
    """-> [{tool, state: added|already|removed|absent|manual|failed, path?, backup?}] for every tool found."""
    out = []
    for name, fn in (("claude", _claude), ("codex", _codex), ("gemini", _gemini), ("opencode", _opencode)):
        if only and name not in only:
            continue
        r = fn(remove)
        if r:
            out.append(r)
    return out


def detected():
    return [r["tool"] for r in run_dry()]


def run_dry():
    """Which tools are installed and not yet connected (changes nothing)."""
    c = cfg()
    out = []
    if shutil.which(c["claude_cli"]):
        if subprocess.run([shutil.which(c["claude_cli"]), "mcp", "get", c["server_name"]], capture_output=True).returncode != 0:
            out.append({"tool": "claude"})
    for tool, d, f, key in (("codex", c["codex_dir"], c["codex_config"], None), ("gemini", c["gemini_dir"], c["gemini_config"], "mcpServers"),
                            ("opencode", c["opencode_dir"], c["opencode_configs"][0], "mcp")):
        if os.path.isdir(os.path.join(home(), d)):
            path = os.path.join(home(), f)
            text = _read(path) if os.path.isfile(path) else ""
            if c["server_name"] not in text:
                out.append({"tool": tool})
    return out

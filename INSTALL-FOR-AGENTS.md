# Install dropviz (instructions for AI assistants)

You were asked to install this repository's tool, `dropviz`, for the user. Do exactly this, then report what you saw.
It needs Python 3.10 or newer and git. It has no other dependencies and makes no network calls of its own.

## 1. Check the basics

```
python3 --version      # Windows: py -3 --version   (must be 3.10 or newer)
git --version
```

If Python is older than 3.10 or missing, stop and tell the user; do not install system packages without asking.

## 2. Install the `dropviz` command

Pick the first that works. All of them install for the current user only (no sudo).

```
pipx install git+https://github.com/zeussoftware-kr/dropviz.git
```

If `pipx` is not installed:

```
python3 -m pip install --user git+https://github.com/zeussoftware-kr/dropviz.git     # Windows: py -3 -m pip install --user ...
```

If you already have a clone of this repository, install from it instead: `python3 -m pip install --user .`

If the `dropviz` command is not found afterwards, the user's scripts folder is not on PATH. You can still run everything as
`python3 -m dropviz ...` (Windows: `py -3 -m dropviz ...`). Do not edit shell profiles without asking.

Check: `dropviz --version` prints `dropviz 0.1.0` (or newer).

## 3. Connect the assistant tools found on this computer

```
dropviz connect
```

It registers dropviz as an MCP server in the AI coding tools it finds (Claude Code through its own `claude mcp` command; Codex,
Gemini CLI and OpenCode through their config files). Each config file is copied to a backup first, running it twice changes nothing,
and `dropviz connect --remove` undoes it. Use `--only claude,codex,gemini,opencode` to limit it. It prints every change it made; show that to the user.
Tell the user to restart their assistant tool so it loads the new MCP server.

## 4. Look at the user's repository

```
dropviz /path/to/the/repo
```

(or `cd` into the repo and run `dropviz .`). It reads the repo read-only, drafts areas from the folders into the git-ignored `.dropviz/`
folder, and serves a map on `http://127.0.0.1:8765/` (it tries to open the browser). Success looks like: the command prints
`dropviz serving http://127.0.0.1:8765/` and that URL shows a map with one tile per feature. Leave it running, or stop it with Ctrl+C.

To try it on invented data first: `python3 examples/make_demo_repo.py /tmp/kestrel` from a clone, then `dropviz /tmp/kestrel`.

## Rules to keep

- Do not change the user's repository files. dropviz writes only `.dropviz/` (git-ignored) until the user saves settings, and then keeps a backup of `dropviz.yaml`.
- Do not push, publish or upload anything. Everything stays on this computer.
- Statuses come only from git evidence (merge into the default branch plus a verification record). Do not tell the user a feature is "done"
  because a session said so; ask dropviz (`status`) instead.
- If a step fails, show the exact error to the user instead of guessing.

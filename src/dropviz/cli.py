import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

from . import __version__, core, defaults, i18n

COMMANDS = ("init", "serve", "mcp", "tokens", "metrics", "timelapse", "configure", "badge", "connect")


def _t(key, **kw):
    return i18n.t(i18n.from_env(os.environ.get("DROPVIZ_LANG")), "cli." + key, **kw)


def is_url(s):
    return bool(re.match(r"^(https?://|git@|ssh://)", s or ""))


def fetch_url(url):
    """A public repo by URL: a history-complete clone without file contents (blobless, no checkout) in the cache."""
    base = os.path.join(defaults.source("cache_dir"), "repos")
    os.makedirs(base, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", re.sub(r"^\w+://|\.git$", "", url)).strip("-")[:60] + "-" + hashlib.sha1(url.encode()).hexdigest()[:8]
    dest = os.path.join(base, key)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if os.path.isdir(os.path.join(dest, ".git")) or os.path.isfile(os.path.join(dest, "HEAD")):
        subprocess.run(["git", "-C", dest, "fetch", "--quiet", "--prune", "origin"], env=env, capture_output=True)
    else:
        p = subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", url, dest], env=env, capture_output=True, text=True)
        if p.returncode:
            raise core.GitError(p.stderr.strip() or _t("git_clone_failed"))
    return dest


def resolve_repo(arg):
    if is_url(arg):
        print(_t("cloning", url=arg), file=sys.stderr, flush=True)
        return core.repo_root(fetch_url(arg))
    return core.repo_root(arg)


def ask_kind():
    """The one question of `init`. Empty answer = a sensible default."""
    try:
        return input(_t("question") + " ").strip()
    except EOFError:
        return ""


def run_connect(remove=False, only=None):
    from . import connect
    res = connect.run(remove, only.split(",") if only else None)
    if not res:
        print(_t("connect_none"))
        return 0
    for r in res:
        print(_t("connect_" + r["state"], tool=r["tool"], path=r.get("path", ""), detail=r.get("detail", ""), line=r.get("line", "")))
        if r.get("backup"):
            print(_t("connect_backup", path=r["backup"]))
    return 1 if any(r["state"] == "failed" for r in res) else 0


def offer_connect():
    """At the end of a first run: one yes/no to connect the AI tools found here (asked once, only at a terminal)."""
    from . import connect
    marker = os.path.join(defaults.source("cache_dir"), defaults.data()["connect"]["offered_marker"])
    if not sys.stdin.isatty() or os.path.exists(marker) or not connect.run_dry():
        return
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w") as f:
        f.write("1\n")
    try:
        yes = input(_t("connect_offer", tools=", ".join(r["tool"] for r in connect.run_dry())) + " ").strip().lower()
    except EOFError:
        return
    if yes in i18n.t(i18n.from_env(), "cli.yes_words").split(","):
        run_connect()


def add_done(p):
    p.add_argument("--done", choices=defaults.data()["done_rules"], help=_t("help_done"))


def build_parser():
    ap = argparse.ArgumentParser(prog="dropviz", description=_t("description"))
    ap.add_argument("--version", action="version", version="dropviz " + __version__)
    ap.add_argument("--lang", help=_t("help_lang"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help=_t("help_init"))
    add_done(p)
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--force", action="store_true", help=_t("help_force"))
    p.add_argument("--kind", help=_t("help_kind"))
    p.add_argument("--no-ask", action="store_true", help=_t("help_no_ask"))
    p = sub.add_parser("serve", help=_t("help_serve"))
    add_done(p)
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--open", action="store_true", help=_t("help_open"))
    p = sub.add_parser("mcp", help=_t("help_mcp"))
    add_done(p)
    p.add_argument("repo", nargs="?", default=".")
    p = sub.add_parser("tokens", help=_t("help_tokens"))
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--commit", help=_t("help_commit"))
    p.add_argument("--branch", help=_t("help_branch"))
    p.add_argument("--since", help=_t("help_since"))
    p.add_argument("--config", help=_t("help_config"))
    p.add_argument("--json", action="store_true", help=_t("help_json"))
    p.add_argument("--write-notes", action="store_true", help=_t("help_notes"))
    p = sub.add_parser("metrics", help=_t("help_metrics"))
    add_done(p)
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--tokens", action="store_true", help=_t("help_metrics_tokens"))
    p.add_argument("--since", help=_t("help_since"))
    p.add_argument("--config", help=_t("help_config"))
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("timelapse", help=_t("help_timelapse"))
    add_done(p)
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--out", required=True, help=_t("help_out"))
    p.add_argument("--view", default=defaults.get("views")[0])
    p.add_argument("--size", default=defaults.get("render_size"), help=_t("help_size"))
    p.add_argument("--fps", type=int, default=None)
    p = sub.add_parser("badge", help=_t("help_badge"))
    add_done(p)
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--out", default=defaults.get("badge_file"), help=_t("help_out"))
    p.add_argument("--url", default=defaults.get("homepage_placeholder"))
    p.add_argument("--tokens", action="store_true", help=_t("help_metrics_tokens"))
    p = sub.add_parser("connect", help=_t("help_connect"))
    p.add_argument("--remove", action="store_true", help=_t("help_remove"))
    p.add_argument("--only", help=_t("help_only"))
    p = sub.add_parser("configure", help=_t("help_configure"))
    add_done(p)
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--areas", help=_t("help_areas"))
    p.add_argument("--size-by", help=_t("help_size_by"))
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `dropviz <path-or-url>` = look at that repo now (the one-command first run)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["serve", argv[0], "--open"] + argv[1:]
    elif not argv:
        argv = ["serve", ".", "--open"]
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.lang:
        os.environ["DROPVIZ_LANG"] = a.lang
    if getattr(a, "done", None):
        os.environ["DROPVIZ_DONE"] = a.done
    if a.cmd == "connect":
        return run_connect(a.remove, a.only)
    try:
        root = resolve_repo(a.repo)
    except (core.GitError, OSError) as e:
        print(_t("not_a_repo", repo=a.repo, err=e), file=sys.stderr)
        return 2
    if a.cmd == "init":
        answer = a.kind if a.kind is not None else ("" if a.no_ask or not sys.stdin.isatty() else ask_kind())
        path, wrote = core.write_draft(root, a.force, answer)
        s = core.compute(root)
        print(_t("wrote" if wrote else "kept", path=path))
        c = s["counts"]
        print(_t("init_counts", total=c["total"], done=c["done"], prog=c["in-progress"], part=c["partly"], unk=c["unknown"], branch=s["default_branch"]))
        print(_t("init_note"))
        return 0
    if a.cmd == "metrics":
        from . import metrics
        tr = None
        if a.tokens:
            import datetime
            from . import tokens
            since = a.since or (datetime.date.today() - datetime.timedelta(days=8 * 7)).isoformat()
            tr = tokens.tokens(root, since=since, config=a.config)
        m = metrics.compute(root, tr)
        print(json.dumps(m, indent=1) if a.json else metrics.format_text(m))
        return 0
    if a.cmd == "tokens":
        from . import tokens
        try:
            res = tokens.tokens(root, since=a.since, commit=a.commit, branch=a.branch, config=a.config)
        except ValueError as e:
            print(_t("error", err=e), file=sys.stderr)
            return 2
        if a.write_notes:
            n = tokens.write_notes(root, res)
            print(_t("wrote_notes", n=n, ref=tokens.NOTES_REF), file=sys.stderr)
        print(json.dumps(res, indent=1, default=str) if a.json else tokens.format_text(root, res))
        return 0
    if a.cmd == "badge":
        from . import badge, tokens
        tot = tokens.summary(tokens.tokens(root))["total"] if a.tokens else None
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(badge.svg(core.compute(root), tokens_total=tot))
        print(_t("badge_wrote", path=a.out))
        print(i18n.t(i18n.from_env(), "badge.snippet", file=a.out, url=a.url, site=defaults.get("site_url")))
        return 0
    if a.cmd == "configure":
        s = {}
        if a.size_by:
            s["size_by"] = a.size_by
        if a.areas:
            s["areas"] = [{"name": n.strip()} for n in a.areas.split(",") if n.strip()]
        try:
            res = core.save_settings(root, s) if s else {"areas": core.area_tree_out(core.compute(root)["areas"])}
        except ValueError as e:
            print(_t("error", err=e), file=sys.stderr)
            return 2
        print(json.dumps(res, indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "timelapse":
        from . import render
        try:
            out = render.timelapse(root, a.out, a.view, a.size, a.fps)
        except render.RenderError as e:
            print(_t("error", err=e), file=sys.stderr)
            return 2
        print(_t("rendered", path=out))
        return 0
    if a.cmd == "serve":
        from . import web
        if not core.load_config(root):
            core.write_auto_config(root)
            print(_t("auto_draft"), file=sys.stderr)
        offer_connect()
        web.serve(root, a.port, a.open)
        return 0
    from . import mcp
    mcp.serve(root)
    return 0

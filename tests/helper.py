import os
import subprocess
import sys
import tempfile

AT = "@"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ["DROPVIZ_LANG"] = "en"  # messages are asserted in English whatever the machine language is
os.environ.setdefault("DROPVIZ_CACHE_DIR", tempfile.mkdtemp(prefix="dropviz-cache-"))  # tests never touch ~/.cache

ENV = dict(os.environ, GIT_AUTHOR_NAME="Dana Human", GIT_AUTHOR_EMAIL="dana" + AT + "example.invalid",
           GIT_COMMITTER_NAME="Dana Human", GIT_COMMITTER_EMAIL="dana" + AT + "example.invalid",
           GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


def sh(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args), env=ENV, capture_output=True, text=True)
    assert p.returncode == 0, (args, p.stderr)
    return p.stdout.strip()


def write(repo, rel, text):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def commit(repo, msg, rel, text):
    write(repo, rel, text)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", msg)
    return sh(repo, "rev-parse", "HEAD")


def make_repo():
    """main: README(## Alpha, ## Beta), app/, lib/ ; app work merged via --no-ff (verified note);
    lib has a trailer commit on main plus an unmerged codex/ branch."""
    d = tempfile.mkdtemp(prefix="dropviz-test-")
    sh(d, "init", "-q", "-b", "main")
    commit(d, "initial", "README.md", "# Demo\n\n## Alpha\ntext\n\n## Beta\ntext\n")
    commit(d, "app skeleton", "app/main.py", "print(1)\n")
    commit(d, "lib skeleton\n\nCo-Authored-By: Claude <noreply" + AT + "anthropic.com>", "lib/core.py", "x=1\n")
    sh(d, "checkout", "-q", "-b", "feature-app")
    commit(d, "app: add login", "app/login.py", "y=1\n")
    sh(d, "checkout", "-q", "main")
    sh(d, "merge", "-q", "--no-ff", "-m", "Merge feature-app", "feature-app")
    sh(d, "checkout", "-q", "-b", "codex/lib-more")
    commit(d, "lib: more things", "lib/more.py", "z=1\n")
    sh(d, "checkout", "-q", "main")
    return d

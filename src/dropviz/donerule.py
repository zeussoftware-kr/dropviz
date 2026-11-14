"""Which rule marks a feature "done". The default is `strict` (core.py): merged into the default branch AND a
verification record. Two opt-in rules exist for repos that keep no verification records:

  merged      the latest change to the feature's paths is on the default branch, no newer unmerged work touches
              them, and no failing CI result is recorded for it.
  merged+ci   as `merged`, and the checks of that latest commit passed. Checks are read from
              `.dropviz/ci-status.json` first, else from the GitHub API (public repos need no token; a token in
              GITHUB_TOKEN or GH_TOKEN raises the rate limit). No network is used with any other rule.

A feature whose checks cannot be read (no result, rate limit, not a GitHub repo) is NOT done under `merged+ci`;
the reason says so. Results are cached per commit in `.dropviz/` and the number of calls per run is capped."""
import json
import os
import re
import urllib.error
import urllib.request

from . import core, defaults, i18n

PASS = ("pass", "passed", "success", "ok", "green")


def get(cfg=None):
    """The rule in use: --done / DROPVIZ_DONE, then dropviz.yaml `done:`, then the default (strict)."""
    rule = os.environ.get("DROPVIZ_DONE") or (cfg or {}).get("done") or defaults.data()["done_rule"]
    rule = str(rule).strip().lower()
    if rule not in defaults.data()["done_rules"]:
        raise ValueError(i18n.t(i18n.from_env(), "err.done_rule", options=", ".join(defaults.data()["done_rules"]), got=rule))
    return rule


def _github_repo(root):
    url = core.git(root, "remote", "get-url", "origin", check=False).strip()
    m = re.search(r"github\.com[:/]+([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$", url)
    return m.groups() if m else None


class Checks:
    """Passing/failing checks of commits: local record, cache, then the GitHub API within a call budget."""

    def __init__(self, root):
        self.root = root
        self.g = defaults.data()["github"]
        self.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        self.api = os.environ.get("DROPVIZ_GITHUB_API") or self.g["api"]
        self.budget = self.g["max_calls_token"] if self.token else self.g["max_calls_anonymous"]
        self.repo = _github_repo(root)
        self.local = {}
        try:
            from . import metrics
            self.local = metrics.ci_status(root)
        except Exception:
            pass
        self.path = os.path.join(core._cache(root), self.g["cache_file"])
        self.cache = core._read_json(self.path, {})
        self.calls = 0
        self.stopped = False
        self.dirty = False

    def _get(self, path):
        req = urllib.request.Request(self.api.rstrip("/") + path, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "dropviz"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        self.calls += 1
        with urllib.request.urlopen(req, timeout=self.g["timeout_seconds"]) as r:
            left = r.headers.get("X-RateLimit-Remaining")
            if left is not None and left.isdigit() and int(left) < self.g["stop_below_remaining"]:
                self.stopped = True
            return json.loads(r.read().decode("utf-8", "replace"))

    def _ask(self, sha):
        o, r = self.repo
        base = "/repos/%s/%s/commits/%s" % (o, r, sha)
        runs = self._get(base + "/check-runs?per_page=100")
        items = runs.get("check_runs") or []
        if items:
            concl = [i.get("conclusion") for i in items]
            if any(c in ("failure", "cancelled", "timed_out", "action_required", "startup_failure") for c in concl):
                return "fail"
            if all(c in ("success", "neutral", "skipped") for c in concl):
                return "pass"
            return None                      # still running
        st = (self._get(base + "/status").get("state") or "").lower()
        return "pass" if st == "success" else "fail" if st in ("failure", "error") else None

    def result(self, sha):
        """-> ('pass'|'fail'|None, why). None = not known."""
        v = self.local.get(sha) or next((x for k, x in self.local.items() if sha.startswith(k)), None)
        if v:
            return ("pass" if v in PASS else "fail" if v == "fail" else None), "ci-status.json"
        if sha in self.cache:
            return self.cache[sha], "cached"
        if not self.repo:
            return None, "no GitHub origin"
        if self.stopped or self.calls >= self.budget:
            return None, "GitHub rate limit or call budget reached"
        try:
            v = self._ask(sha)
        except (urllib.error.URLError, OSError, ValueError):
            return None, "GitHub not reachable"
        if v:
            self.cache[sha] = v
            self.dirty = True
        return v, "GitHub checks"

    def save(self):
        if self.dirty:
            core._write_json(self.path, self.cache)

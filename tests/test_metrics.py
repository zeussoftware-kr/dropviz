import json
import os
import subprocess
import tempfile
import time
import unittest

from tests.helper import ENV, ROOT, sh, write

from dropviz import metrics

DAY = 86400
NOW = 1760000000 + 40 * DAY  # fixed "now"


def dated(repo, ts, *args):
    env = dict(ENV, GIT_AUTHOR_DATE="%d +0000" % ts, GIT_COMMITTER_DATE="%d +0000" % ts)
    p = subprocess.run(["git", "-C", repo] + list(args), env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def unit(repo, ts, rel, name, merge=True):
    """A merged unit touching `rel`; returns the first-parent commit sha."""
    if merge:
        dated(repo, ts, "checkout", "-q", "-b", name)
        write(repo, rel, "%s %d\n" % (name, ts))
        dated(repo, ts, "add", "-A")
        dated(repo, ts, "commit", "-q", "-m", name)
        dated(repo, ts, "checkout", "-q", "main")
        dated(repo, ts, "merge", "-q", "--no-ff", "-m", "Merge " + name, name)
    else:
        write(repo, rel, "%s %d\n" % (name, ts))
        dated(repo, ts, "add", "-A")
        dated(repo, ts, "commit", "-q", "-m", name)
    return sh(repo, "rev-parse", "HEAD")


def make(weeks_with_items):
    d = tempfile.mkdtemp(prefix="dropviz-metrics-")
    sh(d, "init", "-q", "-b", "main")
    write(d, "dropviz.yaml", 'name: "t"\ndefault_branch: "main"\nfeatures:\n  - id: "app"\n    name: "app"\n'
                             '    paths: ["app/"]\n    target: 10\n')
    dated(d, NOW - 60 * DAY, "add", "-A")
    dated(d, NOW - 60 * DAY, "commit", "-q", "-m", "init")
    shas = []
    for i, w in enumerate(weeks_with_items):
        shas.append(unit(d, NOW - w * 7 * DAY - 3600, "app/f%d.py" % i, "b%d" % i))
    return d, shas


class Headline(unittest.TestCase):
    def test_only_verified_counts(self):
        d, shas = make([5, 4, 3, 2, 1])
        m = metrics.compute(d, now=NOW)
        self.assertEqual(m["headline"]["verified_total"], 0)
        self.assertEqual(m["headline"]["merged_total"], 6)  # five merges + the initial commit
        sh(d, "notes", "--ref=dropviz-verify", "add", "-m", "pass", shas[2])  # covers units 0..2
        m = metrics.compute(d, now=NOW)
        self.assertEqual(m["headline"]["verified_total"], 4)  # initial + units 0..2
        self.assertEqual(sum(w["verified"] for w in m["weeks"]) + 1, 4)  # the initial commit is older than 8 weeks
        self.assertEqual(sum(w["unverified"] for w in m["weeks"]), 2)

    def test_note_on_branch_commit_and_fail_file(self):
        d, shas = make([3, 2, 1])
        branch_commit = sh(d, "rev-parse", shas[1] + "^2")
        sh(d, "notes", "--ref=dropviz-verify", "add", "-m", "pass", branch_commit)
        self.assertEqual(metrics.compute(d, now=NOW)["headline"]["verified_total"], 3)
        os.makedirs(os.path.join(d, ".dropviz"))
        with open(os.path.join(d, ".dropviz", "ci-status.json"), "w") as f:
            json.dump({shas[1]: "fail", shas[2]: "pass"}, f)
        m = metrics.compute(d, now=NOW)
        self.assertEqual(m["headline"]["verified_total"], 3)  # unit 1 failed in CI; init, unit 0 and unit 2 verified
        self.assertTrue(m["ci_status_file"])

    def test_tokens_next_to_it(self):
        d, shas = make([1])
        sh(d, "notes", "--ref=dropviz-verify", "add", "-m", "pass", shas[0])
        day = metrics._day(NOW - 7 * DAY - 3600)
        m = metrics.compute(d, tokens_result={"by_day": {day: 1234}}, now=NOW)
        self.assertEqual(sum(w["tokens"] or 0 for w in m["weeks"]), 1234)
        self.assertIsNone(metrics.compute(d, now=NOW)["weeks"][0]["tokens"])
        self.assertNotIn("$", metrics.format_text(m))


class Eta(unittest.TestCase):
    def test_not_enough_data(self):
        d, shas = make([3, 2])
        sh(d, "notes", "--ref=dropviz-verify", "add", "-m", "pass", shas[-1])
        e = metrics.compute(d, now=NOW)["eta"][0]
        self.assertIsNone(e["eta"])
        self.assertIn("not enough data", e["why"])

    def test_eta_from_weekly_growth(self):
        d, shas = make([4, 3, 2, 1])  # one verified item per week over 4 weeks
        sh(d, "notes", "--ref=dropviz-verify", "add", "-m", "pass", shas[-1])
        e = metrics.compute(d, now=NOW)["eta"][0]
        self.assertEqual(e["done_items"], 4)
        self.assertIn("per week", e["why"])
        self.assertTrue(metrics._day(NOW) < e["eta"] < metrics._day(NOW + 400 * DAY), e)

    def test_no_target_no_eta(self):
        d, shas = make([4, 3, 2, 1])
        with open(os.path.join(d, "dropviz.yaml")) as f:
            t = f.read().replace("    target: 10\n", "")
        write(d, "dropviz.yaml", t)
        self.assertIn("target", metrics.compute(d, now=NOW)["eta"][0]["why"])


class Cli(unittest.TestCase):
    def test_cli_and_mcp(self):
        d, shas = make([1])
        sh(d, "notes", "--ref=dropviz-verify", "add", "-m", "pass", shas[0])
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
        out = subprocess.run(["python3", "-m", "dropviz", "metrics", d, "--json"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["headline"]["verified_total"], 2)
        from dropviz import mcp
        self.assertEqual(mcp.Server(d).call("metrics", {})["headline"]["verified_total"], 2)


if __name__ == "__main__":
    unittest.main()

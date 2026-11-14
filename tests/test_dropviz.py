import json
import os
import subprocess
import sys
import time
import unittest
import urllib.request

from tests import helper
from tests.helper import AT, ENV, make_repo, sh

from dropviz import core, mcp, miniyaml


def feat(state, fid):
    return next(f for f in state["features"] if f["id"] == fid)


class AiTrailers(unittest.TestCase):
    def test_must_catch(self):
        cases = {
            "Claude": "x\n\nCo-Authored-By: Claude <noreply" + AT + "anthropic.com>",
            "Claude ": "x\n\nCo-authored-by: Claude Sonnet 5.5 <someone" + AT + "example.invalid>",
            "Codex": "x\n\nCo-Authored-By: OpenAI Codex <noreply" + AT + "example.invalid>",
            "Gemini": "x\n\nCo-Authored-By: Gemini CLI <g" + AT + "example.invalid>",
            "OpenCode": "x\n\nCo-Authored-By: opencode <noreply" + AT + "example.invalid>",
            "Copilot": "x\n\nCo-authored-by: GitHub Copilot <c" + AT + "example.invalid>",
        }
        for label, msg in cases.items():
            self.assertTrue(core.ai_tools_in_message(msg), label)

    def test_must_not_catch(self):
        humans = [
            "fix bug\n\nThis was discussed with Claude in chat, no code used.",
            "x\n\nCo-Authored-By: Claudia Schmidt <claudia" + AT + "example.invalid>",
            "x\n\nCo-Authored-By: Claude Monet <monet" + AT + "example.invalid>",
            "x\n\nCo-Authored-By: Jane Doe <jane" + AT + "anthropic.com>",
            "x\n\nCo-Authored-By: Alex Gemini-Rossi <alex" + AT + "example.invalid>",
            "add cursor movement and a codex parser",
            "x\n\nSigned-off-by: Dana Human <dana" + AT + "example.invalid>",
        ]
        for msg in humans:
            self.assertEqual(core.ai_tools_in_message(msg), [], msg)

    def test_branch_prefix_fallback(self):
        self.assertEqual(core.classify("plain", ["codex/x"], ["codex/"])[0], "ai")
        self.assertEqual(core.classify("plain", ["feature/x"], ["codex/"])[0], "human")
        self.assertEqual(core.classify("plain", ["mycodex/x"], ["codex/"])[0], "human")


class MiniYaml(unittest.TestCase):
    def test_round_trip(self):
        cfg = {"name": "d", "ai_branch_prefixes": ["a/", "b/"],
               "features": [{"id": "x", "name": "X: y # z", "paths": ["a/", "b c/"], "accept": "ok", "status": "unknown"}]}
        back = miniyaml.loads(miniyaml.dumps_config(cfg))
        self.assertEqual(back["features"][0]["name"], "X: y # z")
        self.assertEqual(back["features"][0]["paths"], ["a/", "b c/"])
        self.assertEqual(back["ai_branch_prefixes"], ["a/", "b/"])


class RepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()
        cls.head_before = sh(cls.repo, "rev-parse", "HEAD")
        cls.refs_before = sh(cls.repo, "for-each-ref")

    def test_1_init_draft_and_read_only(self):
        r = subprocess.run([sys.executable, "-m", "dropviz", "init", self.repo], capture_output=True, text=True,
                           env=dict(ENV, PYTHONPATH=helper.SRC))
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = core.load_config(self.repo)
        ids = [f["id"] for f in cfg["features"]]
        self.assertEqual(ids, ["app", "lib"])  # headings that name no code folder are not drafted
        self.assertTrue(all(f["status"] == "unknown" for f in cfg["features"]))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, ".dropviz", ".gitignore")))
        self.assertEqual(sh(self.repo, "rev-parse", "HEAD"), self.head_before)
        self.assertEqual(sh(self.repo, "for-each-ref"), self.refs_before)
        self.assertNotIn("dropviz.yaml", sh(self.repo, "log", "--all", "--name-only", "--format="))

    def test_2_status_honesty(self):
        s = core.compute(self.repo)
        # merged but never verified -> not done
        self.assertNotEqual(feat(s, "app")["status"], "done")
        self.assertEqual(feat(s, "app")["status"], "unknown")
        self.assertEqual(feat(s, "lib")["status"], "in-progress")  # unmerged codex/ branch
        # a report never makes it done
        core.claim(self.repo, "app", "s1")
        core.report(self.repo, "app", "s1", "finished")
        s = core.compute(self.repo)
        self.assertEqual(feat(s, "app")["status"], "unknown")
        self.assertIsNotNone(feat(s, "app")["reported_done"])
        # a verification record on a merged commit does
        merge = sh(self.repo, "rev-parse", "main")
        sh(self.repo, "notes", "--ref=dropviz-verify", "add", "-m", "pass feature=app", merge)
        s = core.compute(self.repo)
        self.assertEqual(feat(s, "app")["status"], "done")
        self.assertEqual(feat(s, "lib")["status"], "in-progress")  # note was for app only
        # a verification note on an unmerged-only commit does not count
        tip = sh(self.repo, "rev-parse", "codex/lib-more")
        sh(self.repo, "notes", "--ref=dropviz-verify", "add", "-m", "pass", tip)
        self.assertNotEqual(feat(core.compute(self.repo), "lib")["status"], "done")

    def test_3_commit_kinds(self):
        rows = {c["subject"]: c for c in core.compute(self.repo)["commits"]}
        self.assertEqual(rows["lib skeleton"]["kind"], "ai")
        self.assertEqual(rows["lib skeleton"]["tools"], ["Claude"])
        self.assertEqual(rows["lib: more things"]["kind"], "ai")  # via codex/ prefix
        self.assertFalse(rows["lib: more things"]["merged"])
        self.assertEqual(rows["app skeleton"]["kind"], "human")
        self.assertTrue(rows["Merge feature-app"]["merge"])

    def test_4_claims(self):
        now = 1_000_000.0
        ok, _ = core.claim(self.repo, "lib", "A", now=now)
        self.assertTrue(ok)
        ok, cur = core.claim(self.repo, "lib", "B", now=now + 60)
        self.assertFalse(ok)
        self.assertEqual(cur["session"], "A")
        self.assertTrue(core.claim(self.repo, "lib", "A", now=now + 3600)[0])  # same session refreshes
        self.assertFalse(core.claim(self.repo, "lib", "B", now=now + 3600 + 7000)[0])  # 1h57m idle: still held
        self.assertTrue(core.claim(self.repo, "lib", "B", now=now + 3600 + 7300)[0])  # >2h idle: expired

    def test_5_web(self):
        import threading
        from http.server import ThreadingHTTPServer
        from dropviz import web
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.repo))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            self.assertIn("dropviz", urllib.request.urlopen(base + "/").read().decode())
            st = json.loads(urllib.request.urlopen(base + "/api/state").read())
            self.assertEqual(st["counts"]["total"], 2)
            req = urllib.request.Request(base + "/", headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(req)
        finally:
            httpd.shutdown()
            httpd.server_close()


class McpRoundTrip(unittest.TestCase):
    def test_stdio(self):
        repo = make_repo()
        core.write_draft(repo)
        env = dict(ENV, PYTHONPATH=helper.SRC)
        p = subprocess.Popen([sys.executable, "-m", "dropviz", "mcp", repo], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

        def rpc(i, method, params=None):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
            p.stdin.flush()
            return json.loads(p.stdout.readline())

        def tool(i, name, args=None):
            r = rpc(i, "tools/call", {"name": name, "arguments": args or {}})["result"]
            return r["isError"], r["content"][0]["text"]

        try:
            r = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})["result"]
            self.assertEqual(r["serverInfo"]["name"], "dropviz")
            self.assertIn("tools", r["capabilities"])
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            names = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
            self.assertEqual(sorted(names), ["claim", "configure", "explain", "metrics", "next", "report", "status", "tokens"])
            err, text = tool(3, "status")
            self.assertFalse(err)
            st = json.loads(text)
            self.assertEqual(st["counts"]["done"], 0)
            self.assertEqual(st["counts"]["total"], 2)
            err, text = tool(4, "next")
            self.assertEqual(json.loads(text)["next"]["id"], "lib")  # unmerged work first
            self.assertFalse(tool(5, "claim", {"feature": "lib", "session": "me"})[0])
            err, text = tool(6, "claim", {"feature": "lib", "session": "other"})
            self.assertTrue(err)
            self.assertIn("claimed by session me", text)
            err, text = tool(7, "report", {"feature": "lib", "session": "me", "note": "done"})
            self.assertTrue(json.loads(text)["status_unchanged"])
            err, text = tool(8, "explain", {"feature": "lib"})
            self.assertEqual(json.loads(text)["status"], "in-progress")
            self.assertEqual(rpc(9, "nope")["error"]["code"], -32601)
        finally:
            p.stdin.close()
            p.wait(timeout=10)
            p.stdout.close()
            p.stderr.close()


if __name__ == "__main__":
    unittest.main()

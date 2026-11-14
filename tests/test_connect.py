"""dropviz connect: fake home folders and a fake `claude` program; the real user config is never touched."""
import json
import os
import stat
import sys
import tempfile
import unittest

from tests import helper  # noqa: F401  (sets DROPVIZ_LANG)

from dropviz import cli, connect


class Fake:
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="dropviz-home-")
        self.bin = tempfile.mkdtemp(prefix="dropviz-bin-")
        self.log = os.path.join(self.bin, "calls.log")
        self.old = {k: os.environ.get(k) for k in ("DROPVIZ_HOME", "PATH")}
        os.environ["DROPVIZ_HOME"] = self.home
        os.environ["PATH"] = self.bin + os.pathsep + "/usr/bin" + os.pathsep + "/bin"  # no real claude/dropviz on the path

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def fake_claude(self):
        p = os.path.join(self.bin, "claude")
        state = os.path.join(self.bin, "claude.added")
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho \"$*\" >> '%s'\nsub=$(echo \"$*\" | cut -d' ' -f2)\n"
                    "if [ \"$sub\" = get ]; then [ -f '%s' ]; exit $?; fi\n"
                    "if [ \"$sub\" = add ]; then touch '%s'; fi\nif [ \"$sub\" = remove ]; then rm -f '%s'; fi\nexit 0\n"
                    % (self.log, state, state, state))
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def mkdir(self, rel):
        os.makedirs(os.path.join(self.home, rel), exist_ok=True)

    def read(self, rel):
        with open(os.path.join(self.home, rel), encoding="utf-8") as f:
            return f.read()


class Connect(Fake, unittest.TestCase):
    def test_nothing_found(self):
        self.assertEqual(connect.run(), [])
        self.assertEqual(cli.run_connect(), 0)

    def test_codex_toml_added_backed_up_idempotent_and_removed(self):
        self.mkdir(".codex")
        cfg = os.path.join(self.home, ".codex/config.toml")
        with open(cfg, "w") as f:
            f.write('model = "x"\n\n[mcp_servers.other]\ncommand = "o"\n')
        r = connect.run(only=["codex"])
        self.assertEqual(r[0]["state"], "added")
        self.assertTrue(os.path.isfile(r[0]["backup"]))
        text = self.read(".codex/config.toml")
        self.assertIn("[mcp_servers.dropviz]", text)
        self.assertIn("[mcp_servers.other]", text)
        self.assertEqual(connect.run(only=["codex"])[0]["state"], "already")
        self.assertEqual(self.read(".codex/config.toml"), text)
        self.assertEqual(connect.run(remove=True, only=["codex"])[0]["state"], "removed")
        after = self.read(".codex/config.toml")
        self.assertNotIn("dropviz", after)
        self.assertIn("[mcp_servers.other]", after)
        self.assertIn('model = "x"', after)

    def test_gemini_and_opencode_json(self):
        self.mkdir(".gemini")
        self.mkdir(".config/opencode")
        with open(os.path.join(self.home, ".gemini/settings.json"), "w") as f:
            json.dump({"theme": "dark", "mcpServers": {"keep": {"command": "k"}}}, f)
        res = {r["tool"]: r for r in connect.run()}
        self.assertEqual(res["gemini"]["state"], "added")
        self.assertEqual(res["opencode"]["state"], "added")
        g = json.loads(self.read(".gemini/settings.json"))
        self.assertEqual(g["theme"], "dark")
        self.assertIn("keep", g["mcpServers"])
        self.assertEqual(g["mcpServers"]["dropviz"]["args"][-1], "mcp")
        o = json.loads(self.read(".config/opencode/opencode.json"))
        self.assertEqual(o["mcp"]["dropviz"]["command"][-1], "mcp")
        self.assertEqual({r["state"] for r in connect.run()}, {"already"})
        self.assertEqual({r["state"] for r in connect.run(remove=True)}, {"removed"})
        self.assertNotIn("dropviz", self.read(".gemini/settings.json"))
        self.assertIn("keep", self.read(".gemini/settings.json"))

    def test_json_with_comments_is_left_alone(self):
        self.mkdir(".config/opencode")
        path = os.path.join(self.home, ".config/opencode/opencode.jsonc")
        raw = '{\n // my settings\n "theme": "x"\n}\n'
        with open(path, "w") as f:
            f.write(raw)
        r = connect.run(only=["opencode"])[0]
        self.assertEqual(r["state"], "manual")
        self.assertEqual(self.read(".config/opencode/opencode.jsonc"), raw)
        self.assertIn("dropviz", r["line"])

    @unittest.skipIf(sys.platform == "win32", "the fake claude is a /bin/sh script; a Windows stand-in (claude.cmd) is not written yet")
    def test_claude_uses_its_own_cli(self):
        self.fake_claude()
        r = connect.run(only=["claude"])[0]
        self.assertEqual(r["state"], "added")
        calls = open(self.log).read()
        self.assertIn("mcp add dropviz --", calls)
        self.assertEqual(connect.run(only=["claude"])[0]["state"], "already")
        self.assertEqual(connect.run(remove=True, only=["claude"])[0]["state"], "removed")
        self.assertIn("mcp remove dropviz", open(self.log).read())
        self.assertEqual(connect.run(remove=True, only=["claude"])[0]["state"], "absent")

    def test_server_command_runs_this_python_when_dropviz_is_not_on_path(self):
        self.assertEqual(connect.server_command()[:3], [sys.executable, "-m", "dropviz"])

    def test_cli_prints_what_changed_and_dry_detection(self):
        self.mkdir(".codex")
        self.assertEqual([r["tool"] for r in connect.run_dry()], ["codex"])
        self.assertEqual(cli.run_connect(), 0)
        self.assertEqual(connect.run_dry(), [])
        self.assertEqual(cli.run_connect(remove=True), 0)
        self.assertEqual([r["tool"] for r in connect.run_dry()], ["codex"])


if __name__ == "__main__":
    unittest.main()

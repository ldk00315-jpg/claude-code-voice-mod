"""Offline tests for voice_helper.py (no codex, no network).

Run:  uv run --project helper python -m unittest discover -s helper/tests -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import voice_helper as vh  # noqa: E402


class _Log:
    def write(self, kind, text):
        pass


vh.LOG = _Log()
EMITTED = []
vh.emit = lambda obj: EMITTED.append(obj)


class FakeApp(vh.AppServer):
    """AppServer with the codex wire replaced by a recorder."""

    def __init__(self):
        super().__init__("codex.exe", {}, None, interrupt_turns=True)
        self.thread_id = "T"
        self.calls = []

    async def request(self, method, params, timeout=60):
        self.calls.append((method, params))
        return {}


class TrimAnswer(unittest.TestCase):
    def test_short_answer_kept(self):
        self.assertEqual(vh.trim_answer("完了しました。"), "完了しました。")

    def test_code_blocks_and_tables_removed(self):
        text = "結果です。\n```python\nprint(1)\n```\n| a | b |\n|---|---|\n**大事**"
        out = vh.trim_answer(text)
        self.assertNotIn("print(1)", out)
        self.assertIn("(code omitted)", out)
        self.assertNotIn("|", out)
        self.assertNotIn("**", out)

    def test_long_answer_cut_at_sentence(self):
        text = "これは文です。" * 600  # 4200 chars
        out = vh.trim_answer(text, 2000)
        self.assertLessEqual(len(out), 2000 + 60)
        self.assertIn("truncated", out)
        self.assertTrue(out.split("\n")[0].endswith("。"))

    def test_long_english_answer_cut_at_sentence(self):
        out = vh.trim_answer("This is a sentence. " * 300, 2000)
        self.assertLessEqual(len(out), 2000 + 60)
        self.assertTrue(out.split("\n")[0].endswith("."))


class AnswerMessage(unittest.TestCase):
    def test_ok(self):
        m = vh.answer_message("ok", "フォルダを調べて", "4つあります。")
        self.assertTrue(m.startswith("[RESULT]"))
        self.assertIn("4つあります。", m)
        self.assertIn("tell the user", m)

    def test_failures(self):
        for status in ("aborted", "error", "refusal", "timeout", "busy"):
            m = vh.answer_message(status, "x", "")
            self.assertTrue(m.startswith("[FAILED]"), status)
        self.assertTrue(vh.answer_message("ok", "x", "  ").startswith("[FAILED]"))


class Persona(unittest.TestCase):
    def tearDown(self):
        vh.PERSONA = dict(vh.DEFAULT_PERSONA)

    def test_default_is_neutral(self):
        p = vh.load_persona({})
        self.assertEqual(p, vh.DEFAULT_PERSONA)
        prompt = vh.build_realtime_prompt(p)
        self.assertIn("You are the voice front desk", prompt)
        self.assertIn("language the user speaks", prompt)
        self.assertIn("[RESULT]", prompt)
        self.assertIn("[FAILED]", prompt)
        self.assertNotIn("You talk with", prompt)
        self.assertNotIn("None", prompt)
        self.assertEqual(vh.addressee(p), "the user")

    def test_custom_character(self):
        p = vh.load_persona({"voice": "maple", "persona": {
            "name": "Aoi", "userName": "Sam", "language": "Japanese", "style": "cheerful and casual",
            "fillers": ["ちょっと待ってね", "  ", 3], "instructions": "Call the computer 'the lab'.",
            "previewText": "", "unknown": "ignored"}})
        self.assertEqual(p["fillers"], ["ちょっと待ってね", "3"])
        self.assertNotIn("unknown", p)
        prompt = vh.build_realtime_prompt(p, label="docs")
        self.assertIn('You are "Aoi"', prompt)
        self.assertIn("You talk with Sam.", prompt)
        self.assertIn("Always speak Japanese.", prompt)
        self.assertIn("cheerful and casual", prompt)
        self.assertIn('"ちょっと待ってね"', prompt)
        self.assertIn("Call the computer 'the lab'.", prompt)
        self.assertIn('thread named "docs"', prompt)
        self.assertIn("Speak Japanese.", vh.preview_text(p))
        vh.PERSONA = p
        self.assertIn("tell Sam now", vh.answer_message("ok", "x", "done"))

    def test_bad_types_ignored(self):
        p = vh.load_persona({"persona": {"name": 5, "fillers": "not a list"}})
        self.assertEqual(p["name"], "")
        self.assertEqual(p["fillers"], [])
        self.assertEqual(vh.load_persona({"persona": "x"}), vh.DEFAULT_PERSONA)


class DataDir(unittest.TestCase):
    def test_paths_follow_data_dir(self):
        old = vh.DATA_DIR
        with tempfile.TemporaryDirectory() as d:
            try:
                vh.set_data_dir(d)
                self.assertEqual(vh.SETTINGS_PATH, os.path.join(d, "settings.json"))
                self.assertEqual(vh.LOG_DIR, os.path.join(d, "logs"))
                self.assertTrue(vh.CODEX_CWD.startswith(d))
                vh.save_settings({"voice": "maple"})
                vh.save_settings({"persona": {"name": "Aoi"}})
                self.assertEqual(vh.load_settings(), {"voice": "maple", "persona": {"name": "Aoi"}})
            finally:
                vh.set_data_dir(old)

    def test_env_override(self):
        old = os.environ.get("VOICE_MOD_DATA_DIR")
        os.environ["VOICE_MOD_DATA_DIR"] = os.path.join(tempfile.gettempdir(), "vm-test")
        try:
            self.assertEqual(vh.default_data_dir(), os.path.join(tempfile.gettempdir(), "vm-test"))
        finally:
            if old is None:
                os.environ.pop("VOICE_MOD_DATA_DIR")
            else:
                os.environ["VOICE_MOD_DATA_DIR"] = old


class McpOverrides(unittest.TestCase):
    def test_names_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            with open(p, "w", encoding="utf-8") as f:
                f.write('[mcp_servers.clock]\ncommand = "x"\n[mcp_servers."we ird"]\nurl = "http://x"\n'
                        '[features]\napps = true\n')
            names = vh.user_mcp_server_names(p)
        self.assertEqual(names, ["clock", "we ird"])
        self.assertEqual(vh.mcp_disable_overrides(names), ["mcp_servers.clock.enabled=false"])

    def test_missing_config(self):
        self.assertEqual(vh.user_mcp_server_names(r"Z:\nope\config.toml"), [])


class HandoffFlow(unittest.TestCase):
    def setUp(self):
        EMITTED.clear()

    def test_handoff_becomes_request_once(self):
        app = FakeApp()
        app.recent_user.append("フォルダ見て")
        item = {"type": "handoff_request", "handoff_id": "item_1", "item_id": "item_1",
                "input_transcript": "フォルダの中身を調べて", "active_transcript": []}
        app._on_handoff(item)
        app._on_handoff(item)  # duplicate
        reqs = [e for e in EMITTED if e["type"] == "request"]
        self.assertEqual(reqs, [{"type": "request", "id": "item_1", "text": "フォルダの中身を調べて",
                                 "userSaid": ["フォルダ見て"]}])

    def test_handoff_falls_back_to_active_transcript(self):
        app = FakeApp()
        app._on_handoff({"handoff_id": "h2", "input_transcript": "",
                         "active_transcript": [{"role": "user", "text": "ログ見て"}]})
        self.assertEqual(EMITTED[-1]["text"], "user: ログ見て")

    def test_answer_needs_running_realtime(self):
        app = FakeApp()
        res = asyncio.run(app.deliver_answer("item_1", "答え", "ok"))
        self.assertFalse(res["ok"])
        self.assertEqual(app.calls, [])
        self.assertEqual(EMITTED[-1]["type"], "answer_undelivered")

    def test_answer_goes_to_append_text(self):
        app = FakeApp()
        app.realtime_active = True
        app._on_handoff({"handoff_id": "item_1", "input_transcript": "調べて"})

        async def run():
            res = await app.deliver_answer("item_1", "結果は3件です。", "ok")
            app.realtime_active = False  # stops the spoken check task
            await asyncio.sleep(0.6)
            return res

        res = asyncio.run(run())
        self.assertTrue(res["ok"])
        method, params = app.calls[0]
        self.assertEqual(method, "thread/realtime/appendText")
        self.assertEqual(params["threadId"], "T")
        self.assertIn("結果は3件です。", params["text"])
        self.assertTrue(params["text"].startswith("[RESULT]"))
        self.assertIn("調べて", params["text"])


class ServerRequests(unittest.TestCase):
    def test_approvals_declined(self):
        app = FakeApp()
        sent = []

        async def send(obj):
            sent.append(obj)
        app._send = send
        for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                       "execCommandApproval", "mcpServer/elicitation/request", "item/tool/call"):
            asyncio.run(app._answer_server_request({"method": method, "id": 1}))
        self.assertEqual(sent[0]["result"], {"decision": "decline"})
        self.assertEqual(sent[2]["result"], {"decision": "denied"})
        self.assertEqual(sent[3]["result"], {"action": "decline", "content": None})
        self.assertIn("error", sent[4])


if __name__ == "__main__":
    unittest.main()

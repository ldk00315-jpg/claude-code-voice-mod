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


# ---------------------------------------------------------------- security: redaction, HTTP token, MCP

class Redactor(unittest.TestCase):
    SECRETS = [
        "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6",
        "sk-ant-api03-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe",
        "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
        "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOp",
        "AKIA" + "IOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "0123456789abcdef0123456789abcdef01234567",  # 40 hex
        "Q2xhdWRlVm9pY2VNb2RTZWNyZXRWYWx1ZTEyMzQ1Ng9xZ",  # random-looking base64
    ]

    def test_known_formats_redacted(self):
        for secret in self.SECRETS:
            out = vh.redact_secrets(f"before {secret} after")
            self.assertNotIn(secret, out, secret)
            self.assertIn("[REDACTED]", out, secret)
            self.assertTrue(out.startswith("before ") and out.endswith(" after"), out)

    def test_bearer_pem_assignments_connection_strings(self):
        cases = {
            "Authorization: Bearer abc.def-ghi_jkl123": "abc.def-ghi_jkl123",
            "password = hunter2hunter2": "hunter2hunter2",
            'DB_PASSWORD="p@ss w0rd"': "p@ss",
            "api_key: 12ab34cd56": "12ab34cd56",
            '{"client_secret": "shh-very-secret"}': "shh-very-secret",
            "SECRET_TOKEN=abcdef": "abcdef",
            "postgres://admin:S3cr3t!@db.example.com:5432/app": "S3cr3t!",
            "mongodb+srv://user:pa55word@cluster0.example.net/db": "pa55word",
            "Server=x;User Id=sa;Password=Pa55w0rd;": "Pa55w0rd",
        }
        for text, secret in cases.items():
            out = vh.redact_secrets(text)
            self.assertNotIn(secret, out, (text, out))
            self.assertIn("[REDACTED]", out, text)
        pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAx1y2z3\nabc\n-----END RSA PRIVATE KEY-----")
        out = vh.redact_secrets(f"key:\n{pem}\ndone")
        self.assertNotIn("MIIEowIBAAKCAQEA", out)
        self.assertIn("done", out)
        self.assertNotIn("abc123", vh.redact_secrets("-----BEGIN PRIVATE KEY-----\nabc123 (cut off"))

    def test_ordinary_text_kept(self):
        for text in [
            "All 12 tests passed; the build folder holds 3 files.",
            "Edit src/components/VoicePanel.tsx and helper/voice_helper.py",
            "max_tokens = 4096, timeout: 30",
            "The thread id is 01a12599-7641-70d0-bae3-a91311f1ed65.",
            "See https://example.com/docs/getting-started for details.",
            "完了しました。テストは全部通りました。",
            "",
        ]:
            self.assertEqual(vh.redact_secrets(text), text)

    def test_log_redaction_includes_secrets(self):
        self.assertNotIn("ghp_", vh.redact("token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"))
        self.assertIn("a=ice-pwd:[REDACTED]", vh.redact("a=ice-pwd:abcdef"))

    def test_short_hash(self):
        self.assertEqual(len(vh.short_hash("x")), 12)
        self.assertEqual(vh.short_hash("x"), vh.short_hash("x"))
        self.assertNotEqual(vh.short_hash("x"), vh.short_hash("y"))


class AnswerPrivacy(unittest.TestCase):
    def setUp(self):
        EMITTED.clear()

    def test_answer_redacted_before_realtime(self):
        app = FakeApp()
        app.realtime_active = True
        app._on_handoff({"handoff_id": "h1", "input_transcript": "check the env"})
        secret = "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"

        async def run():
            res = await app.deliver_answer("h1", f"The key is {secret}. Done.", "ok")
            app.realtime_active = False
            await asyncio.sleep(0.6)
            return res

        self.assertTrue(asyncio.run(run())["ok"])
        sent = app.calls[0][1]["text"]
        self.assertNotIn(secret, sent)
        self.assertIn("[REDACTED]", sent)
        self.assertIn("Done.", sent)

    def test_append_bodies_not_logged(self):
        lines = []

        class Rec:
            def write(self, kind, text):
                lines.append((kind, text))

        class Pipe:
            def write(self, data):
                pass

            async def drain(self):
                pass

        old = vh.LOG
        vh.LOG = Rec()
        try:
            app = vh.AppServer("codex.exe", {}, None, interrupt_turns=True)
            app.proc = type("P", (), {"stdin": Pipe()})()
            asyncio.run(app._send({"method": "thread/realtime/appendText", "id": 7,
                                   "params": {"threadId": "T", "text": "the whole private answer"}}))
        finally:
            vh.LOG = old
        self.assertEqual(len(lines), 1)
        self.assertNotIn("private answer", lines[0][1])
        self.assertIn("chars=24", lines[0][1])
        self.assertIn("sha256=", lines[0][1])


class McpDisable(unittest.TestCase):
    def test_enabled_names_from_effective_config(self):
        cfg = {"mcp_servers": {"a": {"enabled": False}, "b": {"command": "x"}, "c": {"enabled": True},
                               "d.e": {}}}
        self.assertEqual(vh.enabled_mcp_server_names(cfg), ["b", "c", "d.e"])
        self.assertEqual(vh.enabled_mcp_server_names({}), [])
        self.assertEqual(vh.thread_mcp_overrides(["b", "c", "d.e"]),
                         {"mcp_servers.b.enabled": False, "mcp_servers.c.enabled": False})

    def test_handshake_passes_thread_overrides(self):
        app = FakeApp()

        async def request(method, params, timeout=60):
            app.calls.append((method, params))
            if method == "config/read":
                return {"config": {"mcp_servers": {"proj": {"command": "x"}, "off": {"enabled": False}}}}
            if method == "thread/start":
                return {"thread": {"id": "T1"}}
            return {}

        async def notify(method, params=None):
            pass

        app.request, app.notify = request, notify
        app.check_mcp_servers = lambda: asyncio.sleep(0)
        asyncio.run(app.handshake())
        start = dict(app.calls)["thread/start"]
        self.assertEqual(start["config"], {"mcp_servers.proj.enabled": False})

    def test_started_server_is_reported_once(self):
        EMITTED.clear()
        app = FakeApp()
        asyncio.run(app._on_notification("mcpServer/startupStatus/updated",
                                         {"name": "clock", "status": "starting"}, "{}"))
        asyncio.run(app._on_notification("mcpServer/startupStatus/updated",
                                         {"name": "clock", "status": "starting"}, "{}"))
        self.assertEqual([e for e in EMITTED if e["type"] == "mcp_warning"],
                         [{"type": "mcp_warning", "name": "clock", "status": "starting"}])

    def test_status_list_check(self):
        EMITTED.clear()
        app = FakeApp()

        async def request(method, params, timeout=60):
            return {"data": [{"name": "a", "runtimeStatus": "disabled"}, {"name": "b", "runtimeStatus": "connected"}]}

        app.request = request

        async def run():
            real_sleep = asyncio.sleep
            vh.asyncio.sleep = lambda s: real_sleep(0)
            try:
                await app.check_mcp_servers()
            finally:
                vh.asyncio.sleep = real_sleep

        asyncio.run(run())
        self.assertEqual([e["name"] for e in EMITTED if e["type"] == "mcp_warning"], ["b"])


class HttpAuth(unittest.TestCase):
    """The local HTTP server: per-run token, own Host/Origin only, no CORS."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        cls.old_token = vh.TOKEN
        vh.TOKEN = "tok-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe<&\""
        app = FakeApp()
        bridge = type("B", (), {})()
        bridge.app, bridge.voice, bridge.last_activity = app, "cove", 0
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), vh.make_handler(bridge))
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        vh.TOKEN = cls.old_token
        vh.LABEL = ""

    def call(self, method, path, headers=None, body=None, host=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host or f"127.0.0.1:{self.port}")
            data = json.dumps(body).encode() if body is not None else b""
            if method == "POST":
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders(data if method == "POST" else None)
            res = conn.getresponse()
            payload = res.read()
            self.assertFalse([h for h in res.getheaders() if h[0].lower().startswith("access-control")])
            return res.status, payload
        finally:
            conn.close()

    def test_page_is_open_and_carries_the_token(self):
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        escaped = vh.html.escape(vh.TOKEN, quote=True)
        self.assertIn(f'<meta name="voice-token" content="{escaped}">'.encode(), page)
        self.assertNotIn(vh.TOKEN.encode(), page)  # only in its escaped form

    def test_endpoints_need_the_token(self):
        self.assertEqual(self.call("GET", "/health")[0], 403)
        self.assertEqual(self.call("GET", "/events?since=0")[0], 403)
        self.assertEqual(self.call("GET", "/health", {"X-Voice-Token": "wrong"})[0], 403)
        self.assertEqual(self.call("POST", "/label", body={"label": "x"})[0], 403)
        self.assertEqual(self.call("POST", "/answer", {"X-Voice-Token": "wrong"}, {"id": "1"})[0], 403)
        self.assertEqual(self.call("POST", "/shutdown")[0], 403)
        ok = {"X-Voice-Token": vh.TOKEN}
        status, body = self.call("GET", "/health", ok)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.call("POST", "/label", ok, {"label": "docs"})[0], 200)

    def test_origin_and_host_are_checked(self):
        ok = {"X-Voice-Token": vh.TOKEN}
        own = f"http://127.0.0.1:{self.port}"
        self.assertEqual(self.call("GET", "/health", {**ok, "Origin": own})[0], 200)
        self.assertEqual(self.call("GET", "/health", {**ok, "Origin": f"http://localhost:{self.port}"})[0], 200)
        for origin in (f"http://127.0.0.1:{self.port + 1}", "http://evil.example", "null",
                       f"https://127.0.0.1:{self.port}"):
            self.assertEqual(self.call("GET", "/health", {**ok, "Origin": origin})[0], 403, origin)
            self.assertEqual(self.call("POST", "/label", {**ok, "Origin": origin}, {"label": "x"})[0], 403, origin)
        # DNS rebinding: a foreign Host name is refused, even for the page.
        self.assertEqual(self.call("GET", "/", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.call("GET", "/health", ok, host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.call("GET", "/", host=f"localhost:{self.port}")[0], 200)


if __name__ == "__main__":
    unittest.main()

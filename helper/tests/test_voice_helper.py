"""Offline tests for voice_helper.py (no codex, no network).

Run:  uv run --project helper python -m unittest discover -s helper/tests -v
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import voice_helper as vh  # noqa: E402


class _Log:
    def write(self, kind, text):
        pass


vh.LOG = _Log()
EMITTED = []
REAL_EMIT = vh.emit
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
        self.assertEqual(vh.mcp_disable_overrides(names),
                         ['mcp_servers={clock={enabled=false}, "we ird"={enabled=false}}'])

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
        app._on_handoff({"handoff_id": "item_1", "input_transcript": "調べて"})
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
    """F4: every MCP server off, names with dots included, and fail closed when unsure."""

    def setUp(self):
        EMITTED.clear()

    def test_names_from_effective_config(self):
        cfg = {"mcp_servers": {"a": {"enabled": False}, "b": {"command": "x"}, "c": {"enabled": True},
                               "corp.files": {}}}
        self.assertEqual(vh.all_mcp_server_names(cfg), ["a", "b", "c", "corp.files"])
        self.assertEqual(vh.enabled_mcp_server_names(cfg), ["b", "c", "corp.files"])
        self.assertEqual(vh.all_mcp_server_names({}), [])
        self.assertEqual(vh.thread_mcp_overrides(["a", "corp.files"]),
                         {"mcp_servers": {"a": {"enabled": False}, "corp.files": {"enabled": False}}})
        self.assertEqual(vh.thread_mcp_overrides([]), {})

    def test_cli_override_is_one_toml_inline_table(self):
        import tomllib
        names = ["clock", "corp.files", "service@home", 'we"ird']
        (override,) = vh.mcp_disable_overrides(names)
        key, value = override.split("=", 1)  # codex splits at the first "=" only
        self.assertEqual(key, "mcp_servers")  # no dotted path: names with dots stay whole
        parsed = tomllib.loads("_x_ = " + value)["_x_"]  # how codex parses the value
        self.assertEqual(parsed, {n: {"enabled": False} for n in names})
        self.assertEqual(vh.mcp_disable_overrides([]), [])

    def _app(self, responses):
        app = FakeApp()

        async def request(method, params, timeout=60):
            app.calls.append((method, params))
            r = responses.get(method, {})
            if isinstance(r, Exception):
                raise r
            return r(params) if callable(r) else r

        async def notify(method, params=None):
            pass

        app.request, app.notify = request, notify
        app.thread_id = None
        return app

    OK = {
        "config/read": {"config": {"mcp_servers": {"proj": {"command": "x"}, "corp.files": {"command": "y"},
                                                   "off": {"enabled": False}}}},
        "thread/start": {"thread": {"id": "T1"}},
        "mcpServerStatus/list": {"data": [{"name": "proj", "runtimeStatus": "disabled"},
                                          {"name": "corp.files", "runtimeStatus": "disabled"},
                                          {"name": "off", "runtimeStatus": "disabled"}], "nextCursor": None},
    }

    def test_handshake_disables_every_server_and_checks_the_status_list(self):
        app = self._app(dict(self.OK))
        app.cli_mcp_names = ["clock"]
        asyncio.run(app.handshake())
        start = dict(app.calls)["thread/start"]
        self.assertEqual(start["config"], {"mcp_servers": {n: {"enabled": False}
                                                           for n in ["clock", "corp.files", "off", "proj"]}})
        methods = [m for m, _ in app.calls]
        self.assertLess(methods.index("thread/start"), methods.index("mcpServerStatus/list"))

    def test_status_list_is_paged(self):
        pages = {None: {"data": [{"name": "a", "runtimeStatus": "disabled"}], "nextCursor": "1"},
                 "1": {"data": [{"name": "b", "runtimeStatus": "connected"}], "nextCursor": None}}
        app = self._app({**self.OK, "mcpServerStatus/list": lambda p: pages[p.get("cursor")]})
        with self.assertRaises(vh.McpIsolationError) as cm:
            asyncio.run(app.handshake())
        self.assertIn("'b': 'connected'", str(cm.exception))

    def test_fail_closed(self):
        cases = {
            "config/read error": {**self.OK, "config/read": vh.RpcError("boom")},
            "config/read without config": {**self.OK, "config/read": {}},
            "status list error": {**self.OK, "mcpServerStatus/list": vh.RpcError("boom")},
            "status list without data": {**self.OK, "mcpServerStatus/list": {}},
        }
        for name in ("starting", "connected", "notStarted", "failed", "authenticationRequired", None):
            cases[f"server {name}"] = {**self.OK, "mcpServerStatus/list": {
                "data": [{"name": "corp.files", "runtimeStatus": name}]}}
        for label, responses in cases.items():
            app = self._app(responses)
            with self.assertRaises(vh.McpIsolationError, msg=label):
                asyncio.run(app.handshake())

    def test_startup_notification_blocks_realtime(self):
        app = FakeApp()
        app.realtime_active = True

        async def run():
            await app._on_notification("mcpServer/startupStatus/updated",
                                       {"name": "corp.files", "status": "starting"}, "{}")
            await asyncio.sleep(0.05)  # the stop runs as its own task
            with self.assertRaises(vh.McpIsolationError):
                await app.start_realtime("v=0", "v3", "cove")

        asyncio.run(run())
        self.assertFalse(app.realtime_active)
        self.assertIn("thread/realtime/stop", [m for m, _ in app.calls])
        self.assertNotIn("thread/realtime/start", [m for m, _ in app.calls])
        blocked = [e for e in EMITTED if e["type"] == "mcp_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("corp.files", blocked[0]["reason"])


class RealtimeLock(unittest.TestCase):
    """F6: start and stop of the realtime session run one at a time."""

    def test_starts_and_stops_do_not_interleave(self):
        app = FakeApp()
        order = []

        async def request(method, params, timeout=60):
            order.append(method + ">")
            await asyncio.sleep(0.01)
            if method == "thread/realtime/start":
                app._sdp_wait.set_result("answer-sdp")
            order.append(method + "<")
            return {}

        app.request = request

        async def run():
            await asyncio.gather(app.start_realtime("o1", "v3", "cove"), app.stop_realtime(),
                                 app.start_realtime("o2", "v3", "cove"))

        asyncio.run(run())
        for i in range(0, len(order), 2):  # every call ends before the next one starts
            self.assertEqual(order[i][:-1], order[i + 1][:-1], order)
        self.assertEqual(order[-2:], ["thread/realtime/start>", "thread/realtime/start<"])


class SpokenResendGeneration(unittest.TestCase):
    """R2 (third review): the delayed appendSpeech re-send never reaches a newer realtime session."""

    def setUp(self):
        EMITTED.clear()
        self.saved_check = vh.ANSWER_SPOKEN_CHECK_SECONDS
        vh.ANSWER_SPOKEN_CHECK_SECONDS = 1  # re-send after 1 s instead of 12

    def tearDown(self):
        vh.ANSWER_SPOKEN_CHECK_SECONDS = self.saved_check

    def _app(self):
        app = FakeApp()

        async def request(method, params, timeout=60):  # records every RPC, answers start's SDP
            app.calls.append((method, params))
            if method == "thread/realtime/start":
                app._sdp_wait.set_result("answer-sdp")
            return {}

        app.request = request
        return app

    def _methods(self, app):
        return [m for m, _ in app.calls]

    def test_stop_and_start_during_the_wait_drop_the_resend(self):
        app = self._app()

        async def run():
            await app.start_realtime("o1", "v3", "cove")
            app._on_handoff({"handoff_id": "h1", "input_transcript": "check the logs"})
            res = await app.deliver_answer("h1", "Three errors.", "ok")
            self.assertTrue(res["ok"])
            self.assertEqual(len(app._spoken_tasks), 1)
            await asyncio.sleep(0.2)
            await app.stop_realtime()  # real stop and start while the re-send waits
            await app.start_realtime("o2", "v3", "cove")
            self.assertEqual(len(app._spoken_tasks), 0)  # cancelled
            await asyncio.sleep(1.5)  # past the re-send time
            # The old request is refused for the new session as well.
            late = await app.deliver_answer("h1", "Three errors.", "ok")
            self.assertTrue(late.get("refused"))

        asyncio.run(run())
        self.assertNotIn("thread/realtime/appendSpeech", self._methods(app))
        self.assertEqual(self._methods(app).count("thread/realtime/appendText"), 1)

    def test_generation_is_checked_even_if_the_task_was_not_cancelled(self):
        app = self._app()

        async def run():
            await app.start_realtime("o1", "v3", "cove")
            app._on_handoff({"handoff_id": "h1", "input_transcript": "check the logs"})
            await app.deliver_answer("h1", "Three errors.", "ok")
            app._spoken_tasks.clear()  # as if the task had escaped the cancel
            await asyncio.sleep(0.2)
            await app.stop_realtime()
            await app.start_realtime("o2", "v3", "cove")
            await asyncio.sleep(1.5)

        asyncio.run(run())
        self.assertNotIn("thread/realtime/appendSpeech", self._methods(app))

    def test_resend_still_goes_out_in_the_same_session(self):
        app = self._app()

        async def run():
            await app.start_realtime("o1", "v3", "cove")
            app._on_handoff({"handoff_id": "h1", "input_transcript": "check the logs"})
            await app.deliver_answer("h1", "Three errors.", "ok")
            await asyncio.sleep(1.5)

        asyncio.run(run())
        self.assertEqual(self._methods(app).count("thread/realtime/appendSpeech"), 1)

    def test_initial_answer_waits_for_a_running_start_and_is_then_refused(self):
        app = self._app()
        gate = asyncio.Event()

        async def run():
            await app.start_realtime("o1", "v3", "cove")
            app._on_handoff({"handoff_id": "h1", "input_transcript": "check the logs"})
            real = app.request

            async def slow(method, params, timeout=60):
                if method == "thread/realtime/start":
                    await gate.wait()
                return await real(method, params, timeout)

            app.request = slow
            restart = asyncio.create_task(app.start_realtime("o2", "v3", "cove"))
            await asyncio.sleep(0.05)  # the restart holds the lock; the answer must wait
            answer = asyncio.create_task(app.deliver_answer("h1", "Three errors.", "ok"))
            await asyncio.sleep(0.05)
            gate.set()
            await restart
            return await answer

        res = asyncio.run(run())
        self.assertTrue(res.get("refused"))
        self.assertNotIn("thread/realtime/appendText", self._methods(app))


class SecretAssignments(unittest.TestCase):
    """F5: a secret-named value is hidden whatever its form, short numbers included."""

    def test_secret_names_hide_any_value(self):
        cases = [
            ("password = 123456", "123456"), ("password=1234", "1234"), ("PIN: 0000", "0000"),
            ('{"pin": 1234}', "1234"), ("{'password': '42'}", "42"), ("DB_PASSWORD=987654", "987654"),
            ("export API_KEY=12345", "12345"), ("apiKey: \"9\"", "9"), ("token=7", "=7"),
            ("passcode: 246810", "246810"), ("passwd: 1", ": 1"), ('"api_key":"12"', "12"),
            ("The password is 123456.", "123456"), ("my pin is 4321", "4321"), ("PIN is 7", "is 7"),
            ("secret is hunter2", "hunter2"), ("pwd = 99", "99"), ("clientSecret=\"x y z\"", "x y z"),
            ("id:token=abc", "abc"), ("PASSWORD: 'p@ss w0rd'", "p@ss"),
            ("パスワードは123456です", "123456"), ("暗証番号：1234", "1234"), ("PINコードは「0042」", "0042"),
        ]
        for text, secret in cases:
            out = vh.redact_secrets(text)
            self.assertNotIn(secret, out, (text, out))
            self.assertIn("[REDACTED]", out, text)

    def test_harmless_names_keep_numbers(self):
        for text in ["max_tokens = 4096", "timeout: 30", "port=8080", "token_count: 12", "prompt_tokens: 12",
                     '"max_output_tokens": 2048', "The password is wrong.", "the token is expired"]:
            self.assertEqual(vh.redact_secrets(text), text)
        self.assertEqual(vh.redact_secrets("The password is 123456."), "The password is [REDACTED].")

    def test_send_path_hides_a_short_numeric_password(self):
        app = FakeApp()
        app.realtime_active = True
        app._on_handoff({"handoff_id": "h1", "input_transcript": "what is the door code"})

        async def run():
            res = await app.deliver_answer("h1", "The password is 482913 and pin: 77.", "ok")
            app.realtime_active = False
            await asyncio.sleep(0.6)
            return res

        self.assertTrue(asyncio.run(run())["ok"])
        sent = app.calls[0][1]["text"]
        self.assertNotIn("482913", sent)
        self.assertNotIn("77", sent)


class Logging(unittest.TestCase):
    """F5: no conversation text in the log by default; retention at startup."""

    def setUp(self):
        self.old = (vh.LOG, vh.DEBUG_LOG, vh.emit, vh.DATA_DIR)
        self.dir = tempfile.TemporaryDirectory()
        vh.set_data_dir(self.dir.name)
        vh.emit = REAL_EMIT

    def tearDown(self):
        if isinstance(vh.LOG, vh.Log):
            vh.LOG.close()
        vh.LOG, vh.DEBUG_LOG, vh.emit = self.old[0], self.old[1], self.old[2]
        vh.set_data_dir(self.old[3])
        self.dir.cleanup()

    def _log_text(self):
        vh.LOG.close()
        with open(vh.LOG.path, encoding="utf-8") as f:
            return f.read()

    def _talk(self):
        old_out = sys.stdout
        sys.stdout = type("Null", (), {"buffer": io.BytesIO()})()
        try:
            vh.emit({"type": "transcript", "role": "user", "text": "my secret plan for friday"})
            vh.emit({"type": "request", "id": "h1", "text": "read the diary", "userSaid": ["read my diary"]})
            vh.emit({"type": "ready", "port": 1, "token": "TOKEN-VALUE", "pageUrl": "http://127.0.0.1:1/?k=KEYVALUE"})
            vh.LOG.write("RPC<", vh.loggable(json.dumps({"method": "thread/realtime/transcript/done",
                                                         "params": {"role": "assistant", "text": "said aloud"}})))
        finally:
            sys.stdout = old_out

    def test_default_keeps_metadata_only(self):
        vh.DEBUG_LOG = False
        vh.LOG = vh.Log()
        self._talk()
        text = self._log_text()
        for private in ("secret plan", "diary", "said aloud", "TOKEN-VALUE", "KEYVALUE"):
            self.assertNotIn(private, text)
        self.assertIn('"type": "transcript"', text)
        self.assertIn('"text": "<25 chars>"', text)
        self.assertIn('"id": "h1"', text)
        self.assertIn("thread/realtime/transcript/done", text)

    def test_debug_log_keeps_text_redacted(self):
        vh.DEBUG_LOG = True
        vh.LOG = vh.Log()
        self._talk()
        vh.LOG.write("OUT", "password = 123456")
        text = self._log_text()
        self.assertIn("secret plan", text)
        self.assertIn("said aloud", text)
        self.assertNotIn("TOKEN-VALUE", text)
        self.assertNotIn("KEYVALUE", text)
        self.assertNotIn("123456", text)

    def test_retention(self):
        logs = os.path.join(self.dir.name, "logs")
        os.makedirs(logs)
        now = time.time()

        def make(name, size, age_days):
            path = os.path.join(logs, name)
            with open(path, "wb") as f:
                f.write(b"x" * size)
            os.utime(path, (now - age_days * 86400, now - age_days * 86400))
            return path

        old = make("voice-20200101-000000.log", 10, 8)
        a = make("voice-a.log", 9 * 1024 * 1024, 3)
        b = make("voice-b.log", 9 * 1024 * 1024, 2)
        c = make("voice-c.log", 9 * 1024 * 1024, 1)
        other = make("not-ours.txt", 10, 30)
        answers = make("voice-answers.jsonl", 2 * 1024 * 1024, 0)
        removed = vh.prune_logs(logs, now=now)
        self.assertFalse(os.path.exists(old))  # older than 7 days
        self.assertFalse(os.path.exists(a))  # oldest, over 20 MB in all
        self.assertTrue(os.path.exists(b) and os.path.exists(c))
        self.assertTrue(os.path.exists(other))  # not the helper's
        self.assertFalse(os.path.exists(answers))
        self.assertTrue(os.path.exists(os.path.join(logs, "voice-answers.1.jsonl")))  # rotated
        self.assertIn(old, removed)


class HttpAuth(unittest.TestCase):
    """The local HTTP server: per-run token, own Host/Origin only, no CORS."""

    @classmethod
    def setUpClass(cls):
        import threading

        cls.old_token, cls.old_dir, cls.old_log = vh.TOKEN, vh.DATA_DIR, vh.LOG
        cls.tmp = tempfile.TemporaryDirectory()
        vh.set_data_dir(cls.tmp.name)
        cls.lines = []
        vh.LOG = type("Rec", (), {"write": lambda self, kind, text: cls.lines.append((kind, text))})()
        vh.TOKEN = "tok-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe<&\""
        cls.loop = asyncio.new_event_loop()
        threading.Thread(target=cls.loop.run_forever, daemon=True).start()
        app = cls.app = FakeApp()
        bridge = type("B", (), {})()
        bridge.app, bridge.voice, bridge.last_activity = app, "cove", 0
        bridge.args = type("A", (), {"open": False, "version": "v3"})()
        bridge.loop = cls.loop
        bridge.run = lambda coro, timeout: asyncio.run_coroutine_threadsafe(coro, cls.loop).result(timeout)
        cls.server = vh.LimitedHTTPServer(("127.0.0.1", 0), vh.make_handler(bridge))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        time.sleep(0.05)
        cls.loop.close()
        vh.TOKEN, vh.LOG = cls.old_token, cls.old_log
        vh.set_data_dir(cls.old_dir)
        cls.tmp.cleanup()
        vh.LABEL = ""

    def call(self, method, path, headers=None, body=None, host=None, raw=None, length=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host or f"127.0.0.1:{self.port}")
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
            if method == "POST":
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(data) if length is None else length))
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders(data if method == "POST" else None)
            res = conn.getresponse()
            payload = res.read()
            self.assertFalse([h for h in res.getheaders() if h[0].lower().startswith("access-control")])
            return res.status, payload
        finally:
            conn.close()

    def test_page_needs_a_one_time_key(self):
        status, page = self.call("GET", "/")
        self.assertEqual(status, 403)
        self.assertNotIn(b"voice-token", page)
        self.assertEqual(self.call("GET", "/?k=guess")[0], 403)
        key = vh.mint_page_key()
        status, page = self.call("GET", f"/?k={key}")
        self.assertEqual(status, 200)
        escaped = vh.html.escape(vh.TOKEN, quote=True)
        self.assertIn(f'<meta name="voice-token" content="{escaped}">'.encode(), page)
        self.assertNotIn(vh.TOKEN.encode(), page)  # only in its escaped form
        self.assertEqual(self.call("GET", f"/?k={key}")[0], 403)  # used up
        self.assertFalse([t for _, t in self.lines if key in t])  # the key is never logged

    def test_reopen_mints_a_new_link(self):
        EMITTED.clear()
        ok = {"X-Voice-Token": vh.TOKEN}
        self.assertEqual(self.call("POST", "/reopen", body={})[0], 403)
        status, body = self.call("POST", "/reopen", ok, {})
        self.assertEqual((status, json.loads(body)["opened"]), (200, False))
        url = [e for e in EMITTED if e["type"] == "page_url"][-1]["pageUrl"]
        path = url.split(f":{self.port}", 1)[1]
        self.assertEqual(self.call("GET", path)[0], 200)
        self.assertEqual(self.call("GET", path)[0], 403)

    def test_body_limits_and_types(self):
        ok = {"X-Voice-Token": vh.TOKEN}
        self.assertEqual(self.call("POST", "/label", ok, raw=b"", length=-1)[0], 400)
        self.assertEqual(self.call("POST", "/label", ok, raw=b"x" * (17 * 1024))[0], 413)
        self.assertEqual(self.call("POST", "/label", ok, raw=b"", length=2 * 1024 * 1024)[0], 413)
        self.assertEqual(self.call("POST", "/label", ok, raw=b"not json")[0], 400)
        self.assertEqual(self.call("POST", "/label", ok, raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.call("POST", "/label", ok, {"label": 5})[0], 400)
        self.assertEqual(self.call("POST", "/say", ok, {"text": "hi", "mode": "shout"})[0], 400)
        self.assertEqual(self.call("POST", "/offer", ok, {"sdp": ["no"]})[0], 400)
        self.assertEqual(self.call("POST", "/answer", ok, {"id": "x", "answer": {"a": 1}})[0], 400)
        self.assertEqual(self.call("GET", "/events?since=abc", ok)[0], 400)

    def test_answer_needs_an_emitted_request_of_this_session(self):
        ok = {"X-Voice-Token": vh.TOKEN}
        app = self.app
        app.requests.clear()
        status, _ = self.call("POST", "/answer", ok, {"id": "made-up", "answer": "x", "status": "ok"})
        self.assertEqual(status, 403)
        self.assertNotIn("made-up", app.requests)  # no entry created
        self.assertFalse(os.path.exists(vh.ANSWERS_PATH))
        app._on_handoff({"handoff_id": "h-old", "input_transcript": "old"})
        app._session_gen += 2  # the realtime session was stopped and started again
        status, _ = self.call("POST", "/answer", ok, {"id": "h-old", "answer": "x", "status": "ok"})
        self.assertEqual(status, 409)
        app._on_handoff({"handoff_id": "h-now", "input_transcript": "now"})
        status, body = self.call("POST", "/answer", ok, {"id": "h-now", "answer": "x", "status": "ok"})
        self.assertEqual(status, 409)  # accepted, but realtime is not running in this test
        self.assertEqual(json.loads(body)["error"], "realtime not running")
        self.assertTrue(os.path.exists(vh.ANSWERS_PATH))

    def test_handler_threads_are_capped(self):
        import socket
        server = vh.LimitedHTTPServer(("127.0.0.1", 0), self.server.RequestHandlerClass, max_handlers=1)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            slow = socket.create_connection(("127.0.0.1", port))
            slow.sendall(b"GET /health HTTP/1.1\r\n")  # holds the only handler (headers never end)
            time.sleep(0.2)
            busy = socket.create_connection(("127.0.0.1", port))
            busy.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            busy.settimeout(5)
            self.assertTrue(busy.recv(100).startswith(b"HTTP/1.0 503"))
            busy.close()
            slow.close()
        finally:
            server.shutdown()
            server.server_close()

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
        # DNS rebinding: a foreign Host name is refused, even for the page with a valid key.
        key = vh.mint_page_key()
        self.assertEqual(self.call("GET", f"/?k={key}", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.call("GET", "/health", ok, host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.call("GET", f"/?k={key}", host=f"localhost:{self.port}")[0], 200)


if __name__ == "__main__":
    unittest.main()

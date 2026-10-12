"""Platform branches of voice_helper.py: the Windows paths stay as they were, and the POSIX
(macOS, experimental) paths are exercised with mocks. These tests run on Windows too: nothing here
starts a browser, signals a real process or needs a Mac. The one real-process test at the end runs
only on a POSIX system.

Run:  uv run --project helper python -m unittest discover -s helper/tests -v
"""
import asyncio
import io
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import voice_helper as vh  # noqa: E402

URL = "http://127.0.0.1:5000/?k=ONE-TIME-KEY"


class RecLog:
    def __init__(self):
        self.lines = []

    def write(self, kind, text):
        self.lines.append((kind, text))


class LogPatch(unittest.TestCase):
    def setUp(self):
        self.old_log = getattr(vh, "LOG", None)
        self.log = RecLog()
        vh.LOG = self.log

    def tearDown(self):
        vh.LOG = self.old_log


class DataDirPerPlatform(unittest.TestCase):
    home = os.path.expanduser("~")

    def test_windows_unchanged(self):
        self.assertEqual(vh.default_data_dir("win32", {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}),
                         os.path.join(r"C:\Users\u\AppData\Local", "claude-code-voice-mod"))
        self.assertEqual(vh.default_data_dir("win32", {}),
                         os.path.join(self.home, ".local", "share", "claude-code-voice-mod"))

    def test_macos_application_support(self):
        # LOCALAPPDATA and XDG_STATE_HOME mean nothing on macOS.
        env = {"LOCALAPPDATA": "/x", "XDG_STATE_HOME": "/state"}
        self.assertEqual(vh.default_data_dir("darwin", env),
                         os.path.join(self.home, "Library", "Application Support", "claude-code-voice-mod"))

    def test_other_posix_uses_xdg_state_home(self):
        self.assertEqual(vh.default_data_dir("linux", {"XDG_STATE_HOME": "/srv/state"}),
                         os.path.join("/srv/state", "claude-code-voice-mod"))
        self.assertEqual(vh.default_data_dir("linux", {}),
                         os.path.join(self.home, ".local", "state", "claude-code-voice-mod"))
        # A relative XDG_STATE_HOME is invalid per the spec and ignored.
        self.assertEqual(vh.default_data_dir("linux", {"XDG_STATE_HOME": "rel/state"}),
                         os.path.join(self.home, ".local", "state", "claude-code-voice-mod"))

    def test_override_wins_everywhere(self):
        target = os.path.abspath(os.path.join("somewhere", "voice-data"))
        for platform in ("win32", "darwin", "linux"):
            self.assertEqual(vh.default_data_dir(platform, {"VOICE_MOD_DATA_DIR": target,
                                                            "LOCALAPPDATA": "/x"}), target)


class PageCommand(unittest.TestCase):
    def mac(self, installed, home="/Users/u"):
        return vh.page_command(URL, "darwin", isdir=lambda p: p in installed, home=home)

    def test_windows_unchanged(self):
        self.assertEqual(vh.page_command(URL, "win32"), ["cmd", "/c", "start", "", "msedge", f"--app={URL}"])

    def test_macos_prefers_chrome(self):
        everything = {f"/Applications/{a}.app" for a in vh.MAC_APP_BROWSERS}
        self.assertEqual(self.mac(everything),
                         ["open", "-na", "/Applications/Google Chrome.app", "--args", f"--app={URL}"])

    def test_macos_each_browser(self):
        for app in vh.MAC_APP_BROWSERS:
            with self.subTest(app=app):
                self.assertEqual(self.mac({f"/Applications/{app}.app"}),
                                 ["open", "-na", f"/Applications/{app}.app", "--args", f"--app={URL}"])

    def test_macos_user_applications_folder(self):
        path = os.path.join("/Users/u", "Applications") + "/Brave Browser.app"
        self.assertEqual(self.mac({path}), ["open", "-na", path, "--args", f"--app={URL}"])

    def test_macos_falls_back_to_the_default_browser(self):
        self.assertEqual(self.mac(set()), ["open", URL])
        self.assertEqual(self.mac({"/Applications/Safari.app", "/Applications/Firefox.app"}), ["open", URL])

    def test_other_posix(self):
        self.assertEqual(vh.page_command(URL, "linux", which=lambda n: "/usr/bin/chromium" if n == "chromium" else None),
                         ["/usr/bin/chromium", f"--app={URL}"])
        self.assertEqual(vh.page_command(URL, "linux", which=lambda n: None), ["xdg-open", URL])

    def test_url_is_one_argument_never_a_shell_string(self):
        tricky = "http://127.0.0.1:5000/?k=a b;c&d"
        argv = vh.page_command(tricky, "darwin", isdir=lambda p: False)
        self.assertEqual(argv, ["open", tricky])


class OpenPage(LogPatch):
    def test_windows_popen_call_unchanged(self):
        calls = []
        vh.open_page(URL, "win32", popen=lambda argv, **kw: calls.append((argv, kw)))
        self.assertEqual(calls, [(["cmd", "/c", "start", "", "msedge", f"--app={URL}"],
                                  {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)})])

    def test_macos_detached_and_silent(self):
        calls = []
        with mock.patch.object(vh.os.path, "isdir", lambda p: p == "/Applications/Microsoft Edge.app"):
            vh.open_page(URL, "darwin", popen=lambda argv, **kw: calls.append((argv, kw)))
        argv, kw = calls[0]
        self.assertEqual(argv, ["open", "-na", "/Applications/Microsoft Edge.app", "--args", f"--app={URL}"])
        self.assertTrue(kw["start_new_session"])
        self.assertTrue(kw["close_fds"])
        # Nothing may reach the helper's stdout (the JSON protocol to the Mod).
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw[stream], subprocess.DEVNULL)
        self.assertNotIn("shell", kw)
        self.assertEqual(self.log.lines, [])  # the URL (one-time key) is not logged

    def test_failure_is_logged_without_the_url(self):
        def boom(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        with mock.patch.object(vh.os.path, "isdir", lambda p: False):
            vh.open_page(URL, "darwin", popen=boom)
        self.assertEqual(len(self.log.lines), 1)
        kind, text = self.log.lines[0]
        self.assertEqual(kind, "ERROR")
        self.assertNotIn("ONE-TIME-KEY", text)
        self.assertNotIn("127.0.0.1", text)


class SpawnOptions(unittest.TestCase):
    def test_windows_no_window(self):
        self.assertEqual(vh.child_spawn_options("nt"),
                         {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)})

    def test_posix_new_session(self):
        self.assertEqual(vh.child_spawn_options("posix"), {"start_new_session": True})

    def test_guard_per_platform(self):
        self.assertIsInstance(vh.child_guard("posix"), vh._PosixGroup)
        if os.name == "nt":
            self.assertIsInstance(vh.child_guard("nt"), vh._JobKiller)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class KillProcessGroup(unittest.TestCase):
    def run_kill(self, behaviour):
        """behaviour(sig, n) -> None or raises; n counts the probes (signal 0)."""
        calls, clock = [], FakeClock()

        def killpg(pgid, sig):
            calls.append((pgid, sig))
            behaviour(sig, sum(1 for _, s in calls if s == 0))

        result = vh.kill_process_group(4242, grace=1.0, killpg=killpg, sleep=clock.sleep, clock=clock)
        return result, calls

    def test_group_already_gone(self):
        def b(sig, n):
            raise ProcessLookupError

        result, calls = self.run_kill(b)
        self.assertEqual(result, "gone")
        self.assertEqual(calls, [(4242, signal.SIGTERM)])

    def test_terminates_on_sigterm(self):
        def b(sig, n):
            if sig == 0 and n >= 3:
                raise ProcessLookupError

        result, calls = self.run_kill(b)
        self.assertEqual(result, "terminated")
        self.assertEqual(calls[0], (4242, signal.SIGTERM))
        self.assertNotIn((4242, getattr(signal, "SIGKILL", 9)), calls)

    def test_sigkill_after_the_grace_period(self):
        result, calls = self.run_kill(lambda sig, n: None)
        self.assertEqual(result, "killed")
        self.assertEqual(calls[0], (4242, signal.SIGTERM))
        self.assertEqual(calls[-1], (4242, getattr(signal, "SIGKILL", 9)))
        self.assertTrue(all(c[1] == 0 for c in calls[1:-1]))

    def test_permission_denied(self):
        def b(sig, n):
            raise PermissionError

        self.assertEqual(self.run_kill(b)[0], "denied")


class Watchdog(unittest.TestCase):
    def test_argv(self):
        argv = vh.watchdog_argv(77, python="/py", script="/p/voice_helper.py")
        self.assertEqual(argv, ["/py", "-I", "/p/voice_helper.py", "--watch-group", "77"])

    def test_kills_the_group_when_the_pipe_closes(self):
        killed = []
        result = vh.watch_group(77, stdin=io.BytesIO(b""), parent=1, getppid=lambda: 1,
                                kill=lambda pgid: killed.append(pgid) or "terminated", poll=0.01)
        self.assertEqual(result, "terminated")
        self.assertEqual(killed, [77])

    def test_kills_the_group_when_re_parented(self):
        r, w = os.pipe()  # kept open while the watchdog runs: only the parent check can fire
        stdin = os.fdopen(r, "rb", buffering=0)
        try:
            ppid = iter([10, 10, 1])
            killed = []
            t0 = time.monotonic()
            vh.watch_group(77, stdin=stdin, parent=10, getppid=lambda: next(ppid, 1),
                           kill=lambda pgid: killed.append(pgid) or "killed", poll=0.01)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertEqual(killed, [77])
        finally:
            os.close(w)  # EOF for the reader thread first, then close our end
            time.sleep(0.05)
            stdin.close()

    def test_main_runs_the_watchdog_without_a_log(self):
        seen = []
        with mock.patch.object(vh, "watch_group", lambda pgid: seen.append(pgid)), \
                mock.patch.object(vh, "Log", side_effect=AssertionError("no log for the watchdog")), \
                mock.patch.object(sys, "argv", ["voice_helper.py", "--watch-group", "321"]):
            vh.main()
        self.assertEqual(seen, [321])


class FakeWatchdogProc:
    def __init__(self):
        self.stdin = mock.Mock()
        self.waited = None

    def wait(self, timeout):
        self.waited = timeout
        return 0


class PosixGroup(LogPatch):
    def test_add_starts_a_detached_watchdog(self):
        calls, proc = [], FakeWatchdogProc()
        g = vh._PosixGroup(popen=lambda argv, **kw: calls.append((argv, kw)) or proc, kill=lambda pgid: "x")
        self.assertTrue(g.add(555))
        argv, kw = calls[0]
        self.assertEqual(argv[1:], ["-I", os.path.abspath(vh.__file__), "--watch-group", "555"])
        self.assertTrue(kw["start_new_session"])
        self.assertTrue(kw["close_fds"])  # must not hold the helper's stdout to the Mod open
        self.assertEqual(kw["stdin"], subprocess.PIPE)
        self.assertEqual(kw["stdout"], subprocess.DEVNULL)
        self.assertEqual(g.pgid, 555)

    def test_close_kills_the_group_and_releases_the_watchdog(self):
        proc, killed = FakeWatchdogProc(), []
        g = vh._PosixGroup(popen=lambda argv, **kw: proc, kill=lambda pgid: killed.append(pgid) or "terminated")
        g.add(555)
        self.assertEqual(g.close(), "terminated")
        self.assertEqual(killed, [555])
        proc.stdin.close.assert_called_once()
        self.assertIsNotNone(proc.waited)
        self.assertIsNone(g.watchdog)

    def test_watchdog_failure_still_kills_on_close(self):
        def boom(argv, **kw):
            raise OSError("no python")

        killed = []
        g = vh._PosixGroup(popen=boom, kill=lambda pgid: killed.append(pgid) or "gone")
        self.assertFalse(g.add(9))
        self.assertEqual(g.close(), "gone")
        self.assertEqual(killed, [9])
        self.assertEqual(self.log.lines[0][0], "WARN")


class FakeCodexProc:
    def __init__(self, exits_on_stdin_close=True):
        self.stdin = mock.Mock()
        self.returncode = None
        self.killed = False
        self._exit = exits_on_stdin_close

    async def wait(self):
        if not self._exit:
            await asyncio.sleep(3600)
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True


class CloseKillsTheGroup(LogPatch):
    def make_app(self, proc, jobs):
        app = vh.AppServer("codex", {}, jobs, interrupt_turns=True)
        app.proc = proc
        return app

    def test_group_swept_even_after_a_clean_exit(self):
        closed = []

        class G(vh._PosixGroup):
            def close(self):
                closed.append(True)
                return "gone"

        proc = FakeCodexProc()
        asyncio.run(self.make_app(proc, G()).close())
        proc.stdin.close.assert_called_once()
        self.assertEqual(closed, [True])
        self.assertFalse(proc.killed)  # the group kill replaces proc.kill on POSIX

    def test_windows_job_path_does_not_touch_groups(self):
        class Job:
            def add(self, pid):
                return True

        proc = FakeCodexProc()
        asyncio.run(self.make_app(proc, Job()).close())
        self.assertFalse(proc.killed)
        self.assertFalse(any("process group" in t for _, t in self.log.lines))


class Signals(unittest.TestCase):
    def test_windows_unchanged(self):
        expected = [signal.SIGINT] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else []) + [signal.SIGTERM]
        self.assertEqual(vh.stop_signals("nt"), expected)

    def test_posix(self):
        expected = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])
        self.assertEqual(vh.stop_signals("posix"), expected)
        self.assertNotIn(getattr(signal, "SIGBREAK", object()), vh.stop_signals("posix"))

    def test_install_registers_each(self):
        seen = []
        handler = object()
        sigs = vh.install_signal_handlers(handler, "posix", register=lambda s, h: seen.append((s, h)))
        self.assertEqual(seen, [(s, handler) for s in sigs])

    def test_parent_gone(self):
        self.assertFalse(vh.parent_gone(10, "posix", getppid=lambda: 10))
        self.assertTrue(vh.parent_gone(10, "posix", getppid=lambda: 1))
        self.assertFalse(vh.parent_gone(10, "nt", getppid=lambda: 1))  # Windows: unchanged checks


@unittest.skipUnless(os.name == "posix", "needs a POSIX system (process groups)")
class RealProcessGroup(unittest.TestCase):
    """Only on macOS/Linux: a real child in its own session, and its grandchild, are both gone."""

    def test_group_kill_reaches_grandchildren(self):
        child = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & wait"], **vh.child_spawn_options())
        try:
            time.sleep(0.3)
            self.assertEqual(os.getpgid(child.pid), child.pid)
            self.assertIn(vh.kill_process_group(child.pid, grace=2.0), ("terminated", "killed"))
            child.wait(5)
            with self.assertRaises(ProcessLookupError):
                os.killpg(child.pid, 0)
        finally:
            if child.poll() is None:
                child.kill()


if __name__ == "__main__":
    unittest.main()

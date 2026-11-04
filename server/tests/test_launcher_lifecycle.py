"""Bounded launcher lifecycle tests; no game launch or live process termination."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import launcher
import bmplaunch


class Clock:
    now = 0.0

    def sleep(self, seconds):
        self.now += seconds
        if self.now > 1000:
            raise AssertionError("unbounded launcher wait")


class GameLifecycleTest(unittest.TestCase):
    def follow(self, windows=True, alive=None, dialog=None, server_exit=None, proc=None):
        clock = Clock()
        server = mock.Mock()
        server.poll.side_effect = lambda: 1 if server_exit is not None and clock.now >= server_exit else None
        with mock.patch.object(launcher, "IS_WIN", windows), \
                mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                mock.patch.object(launcher.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(launcher, "game_alive", side_effect=lambda: alive(clock.now) if alive else False), \
                mock.patch.object(launcher, "game_launcher_running", side_effect=lambda: dialog(clock.now) if dialog else False), \
                mock.patch.object(launcher, "say"):
            result = launcher.wait_for_game(proc, server)
        return result, clock.now

    def test_windows_closes_promptly_after_game_exit(self):
        result, elapsed = self.follow(alive=lambda t: t < 1)
        self.assertTrue(result)
        self.assertEqual(elapsed, 1 + launcher.WINDOWS_GAME_GONE_S)

    def test_linux_preserves_restart_grace(self):
        result, elapsed = self.follow(windows=False, alive=lambda t: t < 3 or 9 <= t < 12)
        self.assertTrue(result)
        self.assertGreaterEqual(elapsed, 12 + launcher.GAME_GONE_S)

    def test_start_without_game_or_dialog_is_bounded(self):
        result, elapsed = self.follow()
        self.assertFalse(result)
        self.assertEqual(elapsed, launcher.GAME_START_S)

    def test_cancelled_launch_dialog_stops_services(self):
        result, elapsed = self.follow(dialog=lambda t: t < 5)
        self.assertFalse(result)
        self.assertEqual(elapsed, 5 + launcher.WINDOWS_GAME_GONE_S)

    def test_open_dialog_may_outlive_startup_budget(self):
        result, elapsed = self.follow(dialog=lambda t: t < 210, alive=lambda t: 210 <= t < 215)
        self.assertTrue(result)
        self.assertEqual(elapsed, 215 + launcher.WINDOWS_GAME_GONE_S)

    def test_server_exit_interrupts_startup_and_active_game_wait(self):
        for alive in (None, lambda t: True):
            with self.subTest(alive=bool(alive)):
                result, elapsed = self.follow(alive=alive, server_exit=2)
                self.assertFalse(result)
                self.assertEqual(elapsed, 2)

    def test_lingering_windows_bootstrap_does_not_extend_actual_game_lifetime(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        result, elapsed = self.follow(alive=lambda t: t < 1, proc=proc)
        self.assertTrue(result)
        self.assertEqual(elapsed, 1 + launcher.WINDOWS_GAME_GONE_S)
        proc.wait.assert_not_called()

    def test_unknown_scan_resets_confirmed_exit_grace(self):
        result, elapsed = self.follow(alive=lambda t: True if t < 1 else None if 2 <= t < 10 else False)
        self.assertTrue(result)
        self.assertEqual(elapsed, 10 + launcher.WINDOWS_GAME_GONE_S)

    def test_unknown_game_or_dialog_keeps_services_until_server_exits(self):
        for alive, dialog in ((lambda t: None, None), (None, lambda t: None)):
            with self.subTest(dialog=bool(dialog)):
                result, elapsed = self.follow(alive=alive, dialog=dialog, server_exit=launcher.GAME_START_S + 10)
                self.assertFalse(result)
                self.assertEqual(elapsed, launcher.GAME_START_S + 10)

    def test_repeated_tasklist_timeouts_do_not_report_live_game_closed(self):
        clock = Clock()
        queries = []

        def tasklist(command, **kwargs):
            queries.append(clock.now)
            if len(queries) in (2, 3):
                clock.sleep(launcher.PROCESS_QUERY_S)
                raise subprocess.TimeoutExpired(command, launcher.PROCESS_QUERY_S)
            output = (b'"BeamNG.drive.x64.exe","1234","Console","1","100 K"\r\n' if clock.now < 20
                      else b'INFO: No tasks are running which match the specified criteria.\r\n')
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with mock.patch.object(launcher, "IS_WIN", True), \
                mock.patch.object(launcher.subprocess, "run", side_effect=tasklist), \
                mock.patch.object(launcher, "windows_game_main_pids", side_effect=lambda pids: pids), \
                mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                mock.patch.object(launcher.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(launcher, "say"):
            self.assertTrue(launcher.wait_for_game(None))
        self.assertEqual(clock.now, 20 + launcher.WINDOWS_GAME_GONE_S)
        self.assertGreater(len(queries), 3, "must recover from the two timeouts before reporting exit")


class WindowsGameHelpersTest(unittest.TestCase):
    def query(self, entries, pids=(10, 11), exception=None):
        result = mock.Mock(returncode=0, stdout=json.dumps(entries).encode())
        with mock.patch.object(launcher.subprocess, "run", return_value=result, side_effect=exception) as run:
            got = launcher.windows_game_main_pids(list(pids))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 3)
        return got

    def test_filters_only_explicit_cef_helpers(self):
        rows = [{"ProcessId": 10, "CommandLine": '"C:\\Game\\BeamNG.drive.x64.exe" -gfx d3d12'},
                {"ProcessId": 11, "CommandLine": 'BeamNG.drive.x64.exe --type=renderer'}]
        self.assertEqual(self.query(rows), [10])
        self.assertEqual(self.query(rows[1], pids=(11,)), [])

    def test_missing_command_line_or_failed_query_retains_candidates(self):
        self.assertEqual(self.query([{"ProcessId": 10, "CommandLine": None}]), [10, 11])
        self.assertEqual(self.query(None), [10, 11])
        self.assertEqual(self.query(None, exception=subprocess.TimeoutExpired("powershell", 3)), [10, 11])

    def test_failed_tasklist_queries_preserve_unknown(self):
        for error in (OSError("query failed"), subprocess.TimeoutExpired("tasklist", 3)):
            with self.subTest(error=type(error).__name__), mock.patch.object(launcher, "IS_WIN", True), \
                    mock.patch.object(launcher.subprocess, "run", side_effect=error):
                self.assertIsNone(launcher.game_pids())
                self.assertIsNone(launcher.game_alive())
                self.assertIsNone(launcher.game_launcher_running())

    def test_nonzero_empty_or_malformed_tasklist_is_unknown(self):
        for code, output in ((1, b"query failed"), (0, b""), (0, b'"BeamNG.drive.x64.exe'),
                             (0, b'"BeamNG.drive.x64.exe","bad"'),
                             (0, b'"BeamNG.drive.x64.exe"'), (0, b'"BeamNG.drive.x64.exe","0"')):
            with self.subTest(code=code, output=output), mock.patch.object(launcher, "IS_WIN", True), \
                    mock.patch.object(launcher.subprocess, "run", return_value=mock.Mock(returncode=code, stdout=output)):
                self.assertIsNone(launcher.game_alive())

    def test_positive_connection_overrides_unknown_process_scan(self):
        with mock.patch.object(launcher, "game_pids", return_value=None), \
                mock.patch.object(launcher, "game_connected", return_value=True):
            self.assertIs(launcher.game_alive(), True)


class InitialGameScanTest(unittest.TestCase):
    def launch(self, states):
        import install
        args = argparse.Namespace(user_dir="game-user", build=None, keep_mod=False, game_dir=None,
                                  no_game=False, extra=[], cache_dir=None, no_gcpatch=False, gfx=None)
        server, process = mock.Mock(), mock.Mock()
        patches = {}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(launcher, "IS_WIN", True))
            scan = stack.enter_context(mock.patch.object(launcher, "game_alive", side_effect=states))
            sleep = stack.enter_context(mock.patch.object(launcher.time, "sleep"))
            for owner, name, result in (
                    (install, "version", "1.8.4-dev.5"), (install, "clear_reload_files", None),
                    (install, "install_mod", "updated"), (install, "reload_running_game", True),
                    (launcher, "title", None), (launcher, "support_notice", None), (launcher, "say", None),
                    (launcher, "row", None), (launcher, "problem", None), (launcher, "data_dir", "test-data"),
                    (launcher, "under_wine", False), (launcher, "windows_game_dir", "game-folder"),
                    (launcher, "ask_game_dir", None), (launcher, "cache_dir_arg", None),
                    (launcher, "server_port_busy", False), (launcher, "start_server", server),
                    (launcher, "wait_server_ready", True), (launcher, "beammp_up", None),
                    (launcher, "watch_game_log", None), (launcher, "start_game", process),
                    (launcher, "wait_for_game", True), (launcher, "stop_server", None)):
                patches[name] = stack.enter_context(mock.patch.object(owner, name, return_value=result))
            launcher.run(args)
        return patches, scan, sleep, server, process

    def test_persistent_unknown_leaves_game_mod_and_services_untouched(self):
        calls, scan, sleep, _, _ = self.launch([None, None, None])
        self.assertEqual(scan.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        calls["problem"].assert_called_once()
        for name in ("clear_reload_files", "install_mod", "reload_running_game", "ask_game_dir",
                     "start_server", "stop_server", "beammp_up", "start_game", "wait_for_game"):
            calls[name].assert_not_called()

    def test_confirmed_absence_after_unknown_resumes_normal_launch(self):
        calls, scan, _, server, process = self.launch([None, False])
        self.assertEqual(scan.call_count, 2)
        calls["clear_reload_files"].assert_called_once_with("game-user")
        calls["start_game"].assert_called_once()
        calls["reload_running_game"].assert_not_called()
        calls["wait_for_game"].assert_called_once_with(process, server)

    def test_confirmed_presence_after_unknown_does_not_launch_again(self):
        calls, scan, _, server, _ = self.launch([None, True])
        self.assertEqual(scan.call_count, 2)
        calls["clear_reload_files"].assert_not_called()
        calls["start_game"].assert_not_called()
        calls["reload_running_game"].assert_called_once_with("game-user")
        calls["wait_for_game"].assert_called_once_with(None, server)


class ServerReadinessTest(unittest.TestCase):
    def test_os_selected_port_accepts_only_a_valid_bound_port(self):
        import tempfile
        proc = mock.Mock()
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "server.log")
            path.write_text("earthd listening on 127.0.0.1:49123 session fixture\n")
            self.assertTrue(launcher.wait_server_ready(proc, path, port=0, timeout=1))

    def test_waits_for_listener_and_fails_when_child_exits(self):
        for dies in (False, True):
            clock = Clock()
            proc = mock.Mock()
            proc.poll.side_effect = lambda: 2 if dies and clock.now >= 0.5 else None
            with tempfile.TemporaryDirectory() as folder:
                log = Path(folder) / "server.log"
                log.write_text("starting\n")
                def advance(seconds):
                    clock.sleep(seconds)
                    if clock.now >= 1:
                        with log.open("a") as f:
                            f.write("earthd listening on 127.0.0.1:47699 session test -> cache\n")
                with mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                        mock.patch.object(launcher.time, "sleep", side_effect=advance), \
                        mock.patch.object(launcher, "server_port_busy") as probe:
                    self.assertEqual(launcher.wait_server_ready(proc, log, 47699, timeout=2), not dies)
                    probe.assert_not_called()
            self.assertLessEqual(clock.now, 1)

    def test_missing_or_wrong_port_marker_times_out(self):
        for contents in (None, "starting\n", "earthd listening on 127.0.0.1:476130 session wrong\n"):
            clock = Clock()
            proc = mock.Mock()
            proc.poll.return_value = None
            with tempfile.TemporaryDirectory() as folder:
                log = Path(folder) / "server.log"
                if contents is not None:
                    log.write_text(contents)
                with mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                        mock.patch.object(launcher.time, "sleep", side_effect=clock.sleep), \
                        mock.patch.object(launcher, "server_port_busy") as probe:
                    self.assertFalse(launcher.wait_server_ready(proc, log, timeout=1))
                    probe.assert_not_called()
            self.assertEqual(clock.now, 1)

    def test_marker_at_end_of_large_log_and_dead_child(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "server.log"
            log.write_bytes(b"startup data\n" * 200000 + b"earthd listening on 127.0.0.1:47613 session ready\n")
            real_open = open
            reads = []
            def tracked_open(*args, **kwargs):
                stream = real_open(*args, **kwargs)
                original_read = stream.read
                def read(size=-1):
                    reads.append(size)
                    return original_read(size)
                stream.read = read
                return stream
            with mock.patch("builtins.open", side_effect=tracked_open):
                self.assertTrue(launcher.wait_server_ready(proc, log, timeout=1))
            self.assertEqual(reads, [65536])
            proc.poll.return_value = 1
            self.assertFalse(launcher.wait_server_ready(proc, log, timeout=1))

    def test_new_child_truncates_old_marker_before_waiting(self):
        clock = Clock()
        proc = mock.Mock()
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "server.log"
            log.write_text("earthd listening on 127.0.0.1:47613 session old\n")
            with mock.patch.object(launcher.subprocess, "Popen", return_value=proc), \
                    mock.patch.object(launcher.threading, "Thread"):
                launcher.start_server("game-user", [], str(log))
            self.assertEqual(log.read_bytes(), b"")
            with mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                    mock.patch.object(launcher.time, "sleep", side_effect=clock.sleep):
                self.assertFalse(launcher.wait_server_ready(proc, log, timeout=1))
        self.assertEqual(clock.now, 1)


class BeammpStartupTest(unittest.TestCase):
    def run_startup(self, ready=True, failure=None, timeout=30):
        proc = mock.Mock()
        args = argparse.Namespace(no_beammp=False, beammp_wait=timeout)
        with ExitStack() as stack:
            for owner, name, value in ((launcher, "IS_WIN", True),):
                stack.enter_context(mock.patch.object(owner, name, value))
            for owner, name, value in ((launcher, "under_wine", False), (launcher, "data_dir", "test-data"),
                                       (launcher.apppaths, "saved_game_dir", None), (bmplaunch, "running", False),
                                       (bmplaunch, "on_launch", True), (bmplaunch, "find", "test/BeamMP-Launcher.exe"),
                                       (bmplaunch, "start", proc)):
                stack.enter_context(mock.patch.object(owner, name, return_value=value))
            wait = stack.enter_context(mock.patch.object(bmplaunch, "wait_ready", return_value=ready, side_effect=failure))
            stop = stack.enter_context(mock.patch.object(bmplaunch, "stop"))
            stack.enter_context(mock.patch.object(launcher, "row"))
            if failure:
                with self.assertRaises(type(failure)):
                    launcher.beammp_up("game-user", "native", args)
            else:
                self.assertIsNone(launcher.beammp_up("game-user", "native", args))
            wait.assert_called_once_with(proc, timeout=timeout)
            stop.assert_called_once_with(proc)

    def test_timeout_falls_back_and_stops_only_owned_process(self):
        self.run_startup(ready=False, timeout=7)

    def test_interrupted_or_failed_wait_cleans_up_owned_process(self):
        for failure in (KeyboardInterrupt(), OSError("readiness failed")):
            with self.subTest(failure=type(failure).__name__):
                self.run_startup(failure=failure)

    def test_wait_setting_is_finite_and_bounded(self):
        for bad in ("0", "-1", "nan", "inf", "601", "bad"):
            with self.assertRaises(argparse.ArgumentTypeError):
                launcher.beammp_wait_seconds(bad)
        self.assertEqual(launcher.beammp_wait_seconds("12.5"), 12.5)


if __name__ == "__main__":
    unittest.main()

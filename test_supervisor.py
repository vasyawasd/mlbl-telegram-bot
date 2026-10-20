import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Avoid importing a real local credential while testing the supervisor module.
os.environ.setdefault("BOT_TOKEN", "test-token")
import supervisor


class SupervisorStateMixin:
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_dir = Path(self.temp_dir.name)
        self.saved_state = {
            "RUNTIME_DIR": supervisor.RUNTIME_DIR,
            "LOCK_FILE": supervisor.LOCK_FILE,
            "PID_FILE": supervisor.PID_FILE,
            "lock_file_handle": supervisor.lock_file_handle,
            "child_proc": supervisor.child_proc,
            "running": supervisor.running,
            "wake_lock_acquired": supervisor.wake_lock_acquired,
            "pid_file_owned": supervisor.pid_file_owned,
            "cleanup_complete": supervisor.cleanup_complete,
        }
        supervisor.RUNTIME_DIR = self.runtime_dir
        supervisor.LOCK_FILE = self.runtime_dir / ".bot.lock"
        supervisor.PID_FILE = self.runtime_dir / ".bot.pid"
        supervisor.lock_file_handle = None
        supervisor.child_proc = None
        supervisor.running = True
        supervisor.shutdown_event.clear()
        supervisor.wake_lock_acquired = False
        supervisor.pid_file_owned = False
        supervisor.cleanup_complete = False

    def tearDown(self):
        supervisor.release_single_instance_lock()
        for name, value in self.saved_state.items():
            setattr(supervisor, name, value)
        supervisor.shutdown_event.clear()
        self.temp_dir.cleanup()


class TestSupervisorHelpers(SupervisorStateMixin, unittest.TestCase):
    def test_missing_token_is_configuration_error(self):
        with patch.dict(os.environ, {"BOT_TOKEN": ""}, clear=False):
            with patch.object(supervisor, "load_env_value", return_value=""):
                self.assertIn("BOT_TOKEN", supervisor.validate_configuration())

    def test_placeholder_token_is_configuration_error(self):
        with patch.dict(os.environ, {"BOT_TOKEN": "replace-with-your-telegram-bot-token"}, clear=False):
            self.assertIn("BOT_TOKEN", supervisor.validate_configuration())

    def test_lock_writes_pid_and_releases_only_own_pid(self):
        self.assertTrue(supervisor.acquire_single_instance_lock())
        metadata = json.loads(supervisor.PID_FILE.read_text(encoding="utf-8"))
        self.assertEqual(metadata["pid"], os.getpid())
        supervisor.release_single_instance_lock()
        self.assertFalse(supervisor.PID_FILE.exists())

    def test_jittered_delay_is_bounded(self):
        with patch("supervisor.random.uniform", return_value=1.1):
            self.assertEqual(supervisor.restart_delay(2.0), 2.2)
        with patch("supervisor.random.uniform", return_value=1.2):
            self.assertEqual(
                supervisor.restart_delay(supervisor.MAX_BACKOFF_SECONDS),
                supervisor.MAX_BACKOFF_SECONDS,
            )

    def test_signal_forwards_to_live_child_and_stops_restarts(self):
        process = MagicMock()
        process.poll.return_value = None
        process.pid = 1234
        supervisor.child_proc = process
        with patch.object(supervisor.os, "name", "nt"), \
             patch.object(supervisor.signal, "CTRL_BREAK_EVENT", signal.SIGTERM, create=True):
            supervisor.handle_termination(signal.SIGTERM, None)
        self.assertFalse(supervisor.running)
        self.assertTrue(supervisor.shutdown_event.is_set())
        process.send_signal.assert_called_once_with(signal.SIGTERM)


class TestSupervisorLifecycle(SupervisorStateMixin, unittest.TestCase):
    def test_permanent_child_exit_is_not_restarted(self):
        child = MagicMock()
        child.wait.return_value = supervisor.EXIT_CONFIG
        with patch.object(supervisor, "validate_configuration", return_value=None), \
             patch.object(supervisor, "acquire_single_instance_lock", return_value=True), \
             patch.object(supervisor, "acquire_wake_lock"), \
             patch.object(supervisor, "cleanup"), \
             patch.object(supervisor, "start_child", return_value=child) as start_child, \
             patch.object(supervisor, "atexit"):
            self.assertEqual(supervisor.run_supervisor(), supervisor.EXIT_CONFIG)
        start_child.assert_called_once()

    def test_invalid_supervisor_configuration_does_not_lock_or_start(self):
        with patch.object(supervisor, "validate_configuration", return_value="missing token"), \
             patch.object(supervisor, "acquire_single_instance_lock") as acquire_lock:
            self.assertEqual(supervisor.run_supervisor(), supervisor.EXIT_CONFIG)
        acquire_lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

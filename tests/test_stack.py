"""Controller tests run without provider credentials or paid API calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("stack", Path(__file__).resolve().parents[1] / "scripts/stack.py")
stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stack)


class StackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"CCP_KIRO_HOME": str(self.root / "state")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.creds = self.root / "token.json"
        self.creds.write_text(json.dumps({"refreshToken": "test-token", "accessToken": "test-access"}))

    def test_snapshots_are_independent_and_use_is_manual(self):
        stack.add_account("paid-a", self.creds)
        self.creds.write_text(json.dumps({"refreshToken": "other-token"}))
        stack.add_account("paid-b", self.creds)
        first = stack.read_accounts()
        self.assertEqual(first["active"], "paid-a")
        self.assertNotEqual(first["accounts"]["paid-a"]["port"], first["accounts"]["paid-b"]["port"])
        self.assertEqual(json.loads(Path(first["accounts"]["paid-a"]["credentialsFile"]).read_text())["refreshToken"], "test-token")
        stack.use_account("paid-b")
        self.assertEqual(stack.read_accounts()["active"], "paid-b")
        with self.assertRaises(ValueError):
            stack.use_account("missing")
        self.assertEqual(stack.read_accounts()["active"], "paid-b")
        with self.assertRaises(ValueError):
            stack.add_account("paid-a", self.creds)

    def test_path_traversal_and_missing_refresh_are_rejected(self):
        with self.assertRaises(ValueError):
            stack.add_account("../outside", self.creds)
        self.creds.write_text(json.dumps({"accessToken": "expired"}))
        with self.assertRaises(ValueError):
            stack.add_account("bad", self.creds)
        self.assertEqual(stack.read_accounts()["accounts"], {})

    def test_oidc_device_registration_is_saved_with_account(self):
        self.creds.write_text(json.dumps({"refreshToken": "r", "clientIdHash": "device-hash"}))
        (self.root / "device-hash.json").write_text(json.dumps({"clientId": "id", "clientSecret": "secret"}))
        stack.add_account("enterprise", self.creds)
        saved = json.loads(Path(stack.read_accounts()["accounts"]["enterprise"]["credentialsFile"]).read_text())
        self.assertEqual(saved["clientSecret"], "secret")
        self.assertNotIn("clientIdHash", saved)

    def test_atomic_selection_remains_valid_for_readers(self):
        stack.add_account("a", self.creds)
        original = stack.read_accounts()
        with patch.object(stack.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                stack.use_account("a")
        self.assertEqual(stack.read_accounts(), original)
        self.assertEqual(list(stack.state_dir().glob(".write-*")), [])

    def test_os_lock_prevents_second_owner_and_releases(self):
        # Check the same lock in a separate interpreter, without launching servers.
        script = f"import sys; sys.path.insert(0, {str(Path(stack.__file__).parent)!r}); import stack;\nwith stack.locked('service.lock', wait=False): print('owned')"
        with stack.locked("service.lock", wait=False):
            result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Another stack operation", result.stderr)
            self.assertFalse(stack.running())
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_caveman_child_gets_separate_local_route_and_arguments(self):
        with patch.object(stack, "ensure_running"), patch.object(stack.shutil, "which", return_value="caveman"), patch.object(stack.subprocess, "call", return_value=13) as call:
            self.assertEqual(stack.run_claude(["--model", "gpt-6-sol", "-p", "spaces & quotes stay intact"]), 13)
        command = call.call_args.args[0]
        self.assertEqual(command[1:], ["claude", "--model", "gpt-6-sol", "-p", "spaces & quotes stay intact"])
        env = call.call_args.kwargs["env"]
        self.assertEqual(env["CAVE_GATEWAY_URL"], "http://127.0.0.1:18764")
        self.assertIn("127.0.0.1:18765", env["CAVE_SSRF_ALLOWLIST"])
        self.assertIn("base_url: http://127.0.0.1:18765", Path(env["CAVEMAN_CONFIG"]).read_text())

    def test_missing_kiro_account_fails_before_launch_and_model_flag_wins(self):
        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "kiro:claude-sonnet-4-5"}):
            self.assertEqual(stack.requested_model(["--model=gpt-6-sol"]), "gpt-6-sol")
            self.assertEqual(stack.requested_model(["--fallback-model", "gpt-6-sol"]), "kiro:claude-sonnet-4-5")
            with patch.object(stack, "ensure_running") as start:
                with self.assertRaises(RuntimeError):
                    stack.run_claude([])
                start.assert_not_called()

    def test_monitor_attaches_to_service_and_releases_its_lock_on_exit(self):
        with patch.object(stack, "ensure_running") as start, patch.object(stack.subprocess, "call", return_value=0) as call:
            self.assertEqual(stack.main(["monitor"]), 0)
        start.assert_called_once()
        self.assertEqual(call.call_args.args[0][1:], ["monitor", "--url", "http://127.0.0.1:18765"])
        with stack.locked("monitor.lock", wait=False):
            pass

    def test_interactive_windows_launch_opens_dashboard_in_separate_console(self):
        stack.state_dir()
        with patch.object(stack.sys, "platform", "win32"), patch.object(stack.sys.stdin, "isatty", return_value=True), patch.object(stack.sys.stdout, "isatty", return_value=True), patch.object(stack.subprocess, "CREATE_NEW_CONSOLE", 16, create=True), patch.object(stack.subprocess, "Popen") as spawn:
            stack.open_monitor()
        command = spawn.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[-1], "monitor")
        self.assertEqual(spawn.call_args.kwargs["creationflags"], 16)
        self.assertEqual(spawn.call_args.kwargs["env"]["CCP_KIRO_HOME"], str(stack.state_dir()))

    def test_existing_dashboard_and_piped_sessions_do_not_open_another_window(self):
        stack.state_dir()
        with patch.object(stack.sys, "platform", "win32"), patch.object(stack.sys.stdin, "isatty", return_value=True), patch.object(stack.sys.stdout, "isatty", return_value=True), patch.object(stack.subprocess, "Popen") as spawn:
            with stack.locked("monitor.lock", wait=False):
                stack.open_monitor()
            spawn.assert_not_called()
        with patch.object(stack.sys, "platform", "win32"), patch.object(stack.sys.stdin, "isatty", return_value=False), patch.object(stack.subprocess, "Popen") as spawn:
            stack.open_monitor()
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()

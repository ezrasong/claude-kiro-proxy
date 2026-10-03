"""Controller tests run without provider credentials or paid API calls."""
import importlib.util
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
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

    def test_delete_inactive_and_active_accounts_preserves_source_and_manual_selection(self):
        stack.add_account("a", self.creds)
        self.creds.write_text(json.dumps({"refreshToken": "account-b-token"}))
        stack.add_account("b", self.creds)
        folder = stack.state_dir() / "accounts" / "b"
        (folder / "gateway.log").write_text("private account log")
        self.assertEqual(stack.main(["account", "delete", "b"]), 0)
        self.assertFalse(folder.exists())
        self.assertEqual(stack.read_accounts()["active"], "a")
        self.assertTrue(self.creds.exists())
        with self.assertRaises(ValueError):
            stack.delete_account("missing")
        with self.assertRaises(ValueError):
            stack.delete_account("../outside")
        stack.add_account("b", self.creds)
        stack.delete_account("a")
        self.assertIsNone(stack.read_accounts()["active"])
        self.assertIn("b", stack.read_accounts()["accounts"])
        stack.use_account("b")
        self.assertEqual(stack.read_accounts()["active"], "b")

    def test_delete_stops_only_owned_worker_before_removing_files(self):
        stack.add_account("a", self.creds)
        self.creds.write_text(json.dumps({"refreshToken": "account-b-token"}))
        stack.add_account("b", self.creds)
        registry = stack.read_accounts()
        registry["accounts"]["b"]["deleting"] = True
        stack.atomic_json(stack.state_dir() / "kiro-accounts.json", registry)
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.addCleanup(stack.stop_child, worker)
        workers = {"b": worker}
        other = object()
        children = [other, worker]
        remove = stack.shutil.rmtree

        def stopped_before_remove(path):
            self.assertIsNotNone(worker.poll())
            remove(path)

        with stack.locked("accounts.lock"), patch.object(stack.shutil, "rmtree", side_effect=stopped_before_remove):
            stack.cleanup_deleted_accounts(registry, workers, children)
        self.assertEqual(workers, {})
        self.assertEqual(children, [other])
        self.assertEqual(list(stack.read_accounts()["accounts"]), ["a"])
        self.assertTrue(Path(registry["accounts"]["a"]["credentialsFile"]).exists())

    def test_failed_cleanup_reserves_name_and_port_until_retry(self):
        stack.add_account("a", self.creds)
        original_port = stack.read_accounts()["accounts"]["a"]["port"]
        with patch.object(stack.shutil, "rmtree", side_effect=PermissionError("locked")), patch.object(stack.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(RuntimeError, "Deletion is pending"):
                stack.delete_account("a")
        self.assertTrue(stack.read_accounts()["accounts"]["a"]["deleting"])
        with self.assertRaises(ValueError):
            stack.use_account("a")
        with self.assertRaises(ValueError):
            stack.add_account("a", self.creds)
        stack.add_account("b", self.creds)
        self.assertNotEqual(stack.read_accounts()["accounts"]["b"]["port"], original_port)
        stack.delete_account("a")
        self.assertNotIn("a", stack.read_accounts()["accounts"])
        self.assertEqual(stack.read_accounts()["active"], "b")

    def test_delete_refuses_redirected_directory_before_mutation(self):
        stack.add_account("a", self.creds)
        original = stack.read_accounts()
        with patch.object(stack.Path, "resolve", return_value=self.root / "outside"), patch.object(stack.shutil, "rmtree") as remove:
            with self.assertRaises(ValueError):
                stack.delete_account("a")
            remove.assert_not_called()
        self.assertEqual(stack.read_accounts(), original)

    def test_oidc_device_registration_is_saved_with_account(self):
        self.creds.write_text(json.dumps({"refreshToken": "r", "clientIdHash": "device-hash"}))
        (self.root / "device-hash.json").write_text(json.dumps({"clientId": "id", "clientSecret": "secret"}))
        stack.add_account("enterprise", self.creds)
        saved = json.loads(Path(stack.read_accounts()["accounts"]["enterprise"]["credentialsFile"]).read_text())
        self.assertEqual(saved["clientSecret"], "secret")
        self.assertNotIn("clientIdHash", saved)

    def cli_database(self):
        database = self.root / "cli.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("CREATE TABLE conversations (body TEXT)")
            connection.execute("INSERT INTO conversations VALUES ('private chat history')")
            connection.execute("INSERT INTO auth_kv VALUES (?, ?)", ("kirocli:odic:token", json.dumps({
                "refresh_token": "cli-refresh", "access_token": "cli-access",
                "region": "us-east-1", "start_url": "https://view.awsapps.com/start"})))
            connection.execute("INSERT INTO auth_kv VALUES (?, ?)", ("kirocli:odic:device-registration", json.dumps({
                "client_id": "cli-client", "client_secret": "cli-secret"})))
        return database

    def test_cli_import_snapshots_auth_only_and_detects_duplicate_credentials(self):
        database = self.cli_database()
        with patch.object(stack, "default_credentials", return_value=(database, "sqlite")):
            self.assertEqual(stack.main(["account", "add", "cli-a"]), 0)
        account = stack.read_accounts()["accounts"]["cli-a"]
        self.assertEqual(account["kind"], "json")
        snapshot = Path(account["credentialsFile"])
        data = json.loads(snapshot.read_text())
        self.assertEqual(data["refreshToken"], "cli-refresh")
        self.assertEqual(data["clientSecret"], "cli-secret")
        self.assertEqual(data["provider"], "BuilderId")
        self.assertNotIn("private chat history", snapshot.read_text())
        with self.assertRaisesRegex(ValueError, "already saved as cli-a"):
            stack.add_account("duplicate", database, "sqlite")
        self.assertFalse((stack.state_dir() / "accounts/duplicate").exists())
        self.creds.write_text(json.dumps({"refreshToken": "cli-refresh"}))
        with self.assertRaisesRegex(ValueError, "already saved as cli-a"):
            stack.add_account("json-duplicate", self.creds)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE auth_kv SET value=? WHERE key=?", (json.dumps({
                "refresh_token": "different-login", "profile_arn": "explicit-profile"}), "kirocli:odic:token"))
        stack.add_account("cli-b", database, "sqlite")
        self.assertEqual(json.loads(snapshot.read_text())["refreshToken"], "cli-refresh")
        other = json.loads(Path(stack.read_accounts()["accounts"]["cli-b"]["credentialsFile"]).read_text())
        self.assertEqual(other["profileArn"], "explicit-profile")

    def test_default_source_prefers_cli_and_logged_out_cli_does_not_fall_back(self):
        folder = self.root / ("Kiro-Cli" if os.name == "nt" else "kiro-cli")
        folder.mkdir()
        database = self.cli_database()
        destination = folder / "data.sqlite3"
        database.rename(destination)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "XDG_DATA_HOME": str(self.root)}):
            self.assertEqual(stack.default_credentials(), (destination, "sqlite"))
            with closing(sqlite3.connect(destination)) as connection, connection:
                connection.execute("DELETE FROM auth_kv")
            with self.assertRaisesRegex(ValueError, "Kiro CLI has no saved login"):
                stack.main(["account", "add", "logged-out"])
        self.assertEqual(stack.read_accounts()["accounts"], {})

    def test_cli_import_rejects_incomplete_registration_and_supports_legacy_keys(self):
        database = self.cli_database()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE auth_kv SET key='codewhisperer:odic:token' WHERE key='kirocli:odic:token'")
            connection.execute("UPDATE auth_kv SET key='codewhisperer:odic:device-registration' WHERE key='kirocli:odic:device-registration'")
            connection.execute("INSERT INTO state VALUES (?, ?)", ("api.codewhisperer.profile", json.dumps({"arn": "state-profile"})))
        self.assertEqual(stack.cli_credentials(database)["profileArn"], "state-profile")
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DELETE FROM auth_kv WHERE key='codewhisperer:odic:device-registration'")
        with self.assertRaisesRegex(ValueError, "device registration is missing"):
            stack.add_account("incomplete", database, "sqlite")
        self.assertEqual(stack.read_accounts()["accounts"], {})

    def test_atomic_selection_remains_valid_for_readers(self):
        stack.add_account("a", self.creds)
        original = stack.read_accounts()
        with patch.object(stack.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                stack.use_account("a")
        self.assertEqual(stack.read_accounts(), original)
        self.assertEqual(list(stack.state_dir().glob(".write-*")), [])

    def test_builder_id_uses_kiro_service_profile_without_overwriting_saved_profiles(self):
        account = {"kind": "json", "credentialsFile": str(self.creds)}
        for credentials, needs_profile in [
            ({"provider": "BuilderId"}, True),
            ({"provider": "BuilderId", "profileArn": "explicit-profile"}, False),
            ({"provider": "Google"}, False),
            ({}, False),
        ]:
            self.creds.write_text(json.dumps(credentials), encoding="utf-8")
            entry = stack.gateway_credentials(account)[0]
            self.assertEqual("profile_arn" in entry, needs_profile)
            self.assertEqual(entry["path"], str(self.creds))
            self.assertEqual(json.loads(self.creds.read_text()), credentials)
        self.assertEqual(stack.gateway_credentials({**account, "kind": "sqlite"}), [{"type": "sqlite", "path": str(self.creds)}])

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
        self.assertEqual(command[1:3], ["claude", "--settings"])
        self.assertEqual(command[4:], ["--model", "gpt-6-sol", "-p", "spaces & quotes stay intact"])
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

    def test_kiro_launcher_passes_gateway_bearer_auth_before_starting_claude(self):
        requests = []

        class Gateway(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                if requests[-1] != ("/v1/models", "Bearer local-test-key"):
                    self.send_error(401)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data": []}')

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Gateway) as gateway:
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            registry = {"active": "paid-a", "accounts": {"paid-a": {
                "baseUrl": f"http://127.0.0.1:{gateway.server_port}", "apiKey": "local-test-key"}}}
            try:
                # Exercise the HTTP contract directly so a broken header fails
                # immediately, without the launcher's startup retry timeout.
                self.assertEqual(stack.http_json(registry["accounts"]["paid-a"]["baseUrl"] + "/v1/models", "local-test-key"), {"data": []})
                with patch.object(stack, "read_accounts", return_value=registry), patch.object(stack, "ensure_running"), patch.object(stack, "open_monitor"), patch.object(stack.shutil, "which", return_value="caveman"), patch.object(stack.subprocess, "call", return_value=0) as call:
                    self.assertEqual(stack.run_claude(["--model", "kiro:claude-sonnet-4-5"]), 0)
                self.assertEqual(call.call_args.args[0][4:], ["--model", "kiro:claude-sonnet-4-5"])
                self.assertEqual(len(requests), 2)
            finally:
                gateway.shutdown()
                thread.join(timeout=5)

    def test_session_route_preserves_custom_settings_and_cleans_up_private_overlay(self):
        original = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787/w/claude", "EXAMPLE": "preserved",
                            "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL": "1"},
                    "permissions": {"defaultMode": "default"},
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-stop"}]}],
                              "PreToolUse": [{"matcher": "Bash", "hooks": []}]}}
        source = self.root / "custom settings.json"
        source.write_text(json.dumps(original), encoding="utf-8")
        for flag in [["--settings", str(source)], ["--settings=" + json.dumps(original)]]:
            with stack.routed_claude_arguments([*flag, "--model", "sonnet", "--", "literal --settings"]) as arguments:
                path = Path(arguments[1])
                settings = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:18764/w/claude")
                self.assertEqual(settings["env"]["ANTHROPIC_AUTH_TOKEN"], "unused")
                self.assertEqual(settings["env"]["_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"], "0")
                self.assertEqual(settings["env"]["CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK"], "1")
                self.assertEqual(settings["env"]["EXAMPLE"], "preserved")
                self.assertEqual(settings["permissions"], original["permissions"])
                self.assertEqual(settings["hooks"]["Stop"][:-1], original["hooks"]["Stop"])
                self.assertEqual(settings["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
                self.assertEqual(settings["hooks"]["Stop"][-1]["hooks"][0]["type"], "prompt")
                self.assertEqual(arguments[2:], ["--model", "sonnet", "--", "literal --settings"])
            self.assertFalse(path.exists())
        self.assertEqual(json.loads(source.read_text()), original)
        for invalid in [["--settings"], ["--settings", '{"env":null}'], ["--settings", '{invalid}'],
                        ["--settings", '{"hooks":null}'], ["--settings", '{"hooks":{"Stop":{}}}']]:
            with self.assertRaises(ValueError), stack.routed_claude_arguments(invalid):
                pass

    def test_continuation_overlay_is_private_to_each_launch(self):
        with stack.routed_claude_arguments([]) as first:
            first_path = Path(first[1])
            with stack.routed_claude_arguments([]) as second:
                second_path = Path(second[1])
                self.assertNotEqual(first_path, second_path)
                self.assertEqual(json.loads(first_path.read_text()), json.loads(second_path.read_text()))
            self.assertFalse(second_path.exists())
            self.assertTrue(first_path.exists())
        self.assertFalse(first_path.exists())

    def test_proxy_route_is_available_before_settings_load_without_changing_shell(self):
        inherited = {"ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                     "ANTHROPIC_AUTH_TOKEN": "unrelated-token",
                     "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL": "1"}
        with patch.dict(os.environ, inherited):
            environment = stack.launch_env()
            self.assertEqual(environment["ANTHROPIC_BASE_URL"], "http://127.0.0.1:18764/w/claude")
            self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "unused")
            self.assertEqual(environment["_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"], "0")
            self.assertEqual(environment["CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK"], "1")
            for name, value in inherited.items():
                self.assertEqual(os.environ[name], value)

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

    def test_worker_unicode_banner_survives_redirected_legacy_windows_encoding(self):
        log = self.root / "unicode-worker.log"
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        child = stack.start_child([sys.executable, "-c", "print('\\U0001f47b gateway')"], env, log)
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertIn("\U0001f47b gateway", log.read_text(encoding="utf-8"))

    def test_gateway_disables_fake_reasoning_and_keeps_account_isolation(self):
        folder = self.root / "worker"
        with patch.dict(os.environ, {"FAKE_REASONING": "true", "REFRESH_TOKEN": "other-account", "KIRO_TOKEN": "other", "ACCOUNTS_CONFIG_FILE": "other.json"}):
            env = stack.gateway_env({"apiKey": "account-key", "port": 18770}, folder)
        self.assertEqual(env["FAKE_REASONING"], "false")
        self.assertEqual(env["STREAMING_READ_TIMEOUT"], "300")
        self.assertEqual(env["PROXY_API_KEY"], "account-key")
        self.assertEqual(env["ACCOUNTS_CONFIG_FILE"], str(folder / "gateway-accounts.json"))
        self.assertEqual(env["ACCOUNTS_STATE_FILE"], str(folder / "gateway-state.json"))
        self.assertEqual(env["DEBUG_MODE"], "off")
        self.assertEqual(env["ACCOUNT_SYSTEM"], "false")
        self.assertNotIn("REFRESH_TOKEN", env)
        self.assertNotIn("KIRO_TOKEN", env)

    def test_gateway_patch_is_repeatable_and_preserves_conflicting_edits(self):
        gateway = self.root / "gateway"
        gateway.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=gateway, check=True)
        (gateway / "sample.txt").write_text("original\n")
        patches = self.root / "patches"
        patches.mkdir()
        (patches / "kiro-gateway.patch").write_text("diff --git a/sample.txt b/sample.txt\n--- a/sample.txt\n+++ b/sample.txt\n@@ -1 +1 @@\n-original\n+fixed\n" + "diff --git a/stream.txt b/stream.txt\n--- a/stream.txt\n+++ b/stream.txt\n@@ -1 +1 @@\n-silent\n+keepalive\n")
        (gateway / "stream.txt").write_text("silent\n")
        (patches / "kiro-native-streaming.patch").write_text("diff --git a/stream.txt b/stream.txt\n--- a/stream.txt\n+++ b/stream.txt\n@@ -1 +1 @@\n-silent\n+keepalive\n")
        with patch.object(stack, "ROOT", self.root), patch.object(stack, "GATEWAY", gateway):
            stack.apply_gateway_patch()
            stack.apply_gateway_patch()
            self.assertEqual((gateway / "sample.txt").read_text(), "fixed\n")
            self.assertEqual((gateway / "stream.txt").read_text(), "keepalive\n")
            # A previous installation already has the refusal fix. Migrate
            # the remaining change without resetting any unrelated files.
            (gateway / "stream.txt").write_text("silent\n")
            (gateway / "local.txt").write_text("keep user changes\n")
            stack.apply_gateway_patch()
            stack.apply_gateway_patch()
            self.assertEqual((gateway / "stream.txt").read_text(), "keepalive\n")
            self.assertEqual((gateway / "local.txt").read_text(), "keep user changes\n")
            (gateway / "sample.txt").write_text("user edit\n")
            with self.assertRaisesRegex(RuntimeError, "resolve local gateway edits"):
                stack.apply_gateway_patch()
            self.assertEqual((gateway / "sample.txt").read_text(), "user edit\n")
            (gateway / "stream.txt").write_text("silent\n")
            with self.assertRaisesRegex(RuntimeError, "resolve local gateway edits"):
                stack.apply_gateway_patch()
            self.assertEqual((gateway / "stream.txt").read_text(), "silent\n")


if __name__ == "__main__":
    unittest.main()

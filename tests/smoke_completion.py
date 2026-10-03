"""Check the real Claude Code task loop against a local mock, without paid calls.

Run: python tests/smoke_completion.py --claude-bin /path/to/claude
The mock deliberately stops early, then completes one permitted echo command.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stack


def run_case(executable, case):
    requests = []
    decisions = []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            if self.path.endswith("/count_tokens"):
                self.respond({"input_tokens": 20})
                return
            requests.append(body)
            system = json.dumps(body.get("system", []))
            evaluator = "evaluating a stop-condition hook" in system
            history = json.dumps(body["messages"])
            has_result = any(block.get("type") == "tool_result" and "PROXY_TOOL_CHECK" in json.dumps(block)
                             for message in body["messages"] if isinstance(message["content"], list)
                             for block in message["content"])
            stop = "end_turn"
            if evaluator:
                # Exercise native feedback and completion, plus the no-progress cap.
                ok = case in ("completed", "blocked") or has_result
                decisions.append(ok)
                text = json.dumps({"ok": ok, "reason": "Evidence: echo completed" if ok else
                                   "Requested echo is still missing. Run echo PROXY_TOOL_CHECK now."})
                content = [{"type": "text", "text": text}]
            elif case == "refusal":
                content = [{"type": "text", "text": "Kiro refused to continue this conversation (CONTENT_FILTERED)."}]
                stop = "refusal"
            elif case == "blocked":
                content = [{"type": "text", "text": "Required access is missing; please provide access to proceed."}]
            elif case == "completed" or has_result:
                content = [{"type": "text", "text": "VERIFIED"}]
            elif case == "unfinished" and "Requested echo is still missing" in history:
                content = [{"type": "tool_use", "id": "tool_check", "name": "Bash",
                            "input": {"command": "echo PROXY_TOOL_CHECK", "description": "Verify continuation"}}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": "I will do the requested work later."}]
            result = {"id": "msg_check", "type": "message", "role": "assistant", "model": body["model"],
                      "content": content, "stop_reason": stop, "stop_sequence": None,
                      "usage": {"input_tokens": 20, "output_tokens": 10}}
            self.respond(result, body.get("stream", False))

        def respond(self, result, stream=False):
            if stream:
                events = [("message_start", {"type": "message_start", "message": {
                    **result, "content": [], "stop_reason": None}})]
                for index, block in enumerate(result["content"]):
                    tool = block["type"] == "tool_use"
                    start = {**block, "input": {}} if tool else {"type": "text", "text": ""}
                    delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if tool else {
                        "type": "text_delta", "text": block["text"]}
                    events.extend([
                        ("content_block_start", {"type": "content_block_start", "index": index, "content_block": start}),
                        ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}),
                        ("content_block_stop", {"type": "content_block_stop", "index": index})])
                events.extend([
                    ("message_delta", {"type": "message_delta", "delta": {
                        "stop_reason": result["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": 10}}),
                    ("message_stop", {"type": "message_stop"})])
                payload = "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in events).encode()
            else:
                payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with ThreadingHTTPServer(("127.0.0.1", 0), Model) as server, tempfile.TemporaryDirectory() as folder:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = {**os.environ, "CCP_KIRO_HOME": str(Path(folder) / "state"),
                   "CLAUDE_CONFIG_DIR": str(Path(folder) / "claude"),
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
            env.pop("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", None)
            hook_script = Path(folder) / "existing_hook.py"
            hook_script.write_text("from pathlib import Path\nimport sys\n"
                                   "with Path(sys.argv[1]).open('a') as f: f.write('.')\n"
                                   "print('{}')\n", encoding="utf-8")

            def existing_hook(marker):
                command = f'"{Path(sys.executable).as_posix()}" "{hook_script.as_posix()}" "{marker.as_posix()}"'
                return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}

            config = Path(env["CLAUDE_CONFIG_DIR"])
            config.mkdir()
            global_marker = Path(folder) / "global-hook-ran"
            supplied_marker = Path(folder) / "supplied-hook-ran"
            user_settings = existing_hook(global_marker)
            user_settings["env"] = {"_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL": "1"}
            (config / "settings.json").write_text(json.dumps(user_settings))
            args = ["--model", "claude-sonnet-4-5", "--tools", "Bash", "--allowedTools", "Bash(echo PROXY_TOOL_CHECK)",
                    "--settings", json.dumps(existing_hook(supplied_marker)),
                    "--setting-sources", "user", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--no-session-persistence", "--output-format", "json", "-p",
                    "Run echo PROXY_TOOL_CHECK exactly once, then reply VERIFIED."]
            with patch.dict(os.environ, env), patch.object(stack, "CAVE_PORT", server.server_port):
                with stack.routed_claude_arguments(["auth", "status"]) as auth_args:
                    auth = subprocess.run([executable, *auth_args], env=env, cwd=folder, capture_output=True,
                                          text=True, encoding="utf-8", timeout=30)
                assert auth.returncode == 0 and json.loads(auth.stdout)["loggedIn"], "Local proxy auth was not recognized"
                with stack.routed_claude_arguments(args) as routed:
                    process = subprocess.run([executable, *routed], env=env, cwd=folder, capture_output=True,
                                             text=True, encoding="utf-8", timeout=120)
            result = json.loads(process.stdout)
            if case == "refusal":
                assert process.returncode == 1 and result.get("stop_reason") == "refusal", result
                assert decisions == [], "Stop checker ran after an API refusal"
                # Claude itself makes one built-in refusal retry, then stops.
                assert len(requests) == 2, "Continuation retried a refusal"
                print("PASS: refusal; continuation did not run")
                return
            assert process.returncode == 0, process.stderr[-1000:]
            assert not result.get("is_error"), result
            assert decisions, "Claude did not run the Stop evaluator"
            for marker in (global_marker, supplied_marker):
                assert marker.exists(), "An existing Stop hook was lost"
                assert len(marker.read_text()) == len(decisions), "An existing hook did not run on each stop"
            if case == "unfinished":
                assert decisions == [False, True], decisions
                assert result["result"] == "VERIFIED", result
                tools = [block for body in requests for message in body["messages"]
                         if isinstance(message["content"], list) for block in message["content"]
                         if block.get("type") == "tool_use"]
                assert any(block["input"]["command"] == "echo PROXY_TOOL_CHECK" for block in tools), tools
            elif case in ("completed", "blocked"):
                assert decisions == [True], decisions
                assert len(requests) == 2, "Completed/blocked work was retried"
            else:
                assert decisions == [False] * 9, decisions
                assert len(requests) == 18, "Native no-progress cap was not preserved"
            print(f"PASS: {case}; {len(decisions)} completion checks")
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin", required=True)
    args = parser.parse_args()
    for scenario in ("unfinished", "completed", "blocked", "refusal", "no-progress"):
        run_case(args.claude_bin, scenario)

"""Exercise installed Caveman -> built Rust proxy -> mock Kiro, without paid calls.

Run: python tests/smoke_caveman.py --caveman-bin /path/to/caveman-proxy
"""
import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caveman-bin", required=True)
    args = parser.parse_args()
    captured = []

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            captured.append((self.path, dict(self.headers), body))
            if self.path.endswith("/count_tokens"):
                payload = json.dumps({"input_tokens": 12}).encode()
                content_type = "application/json"
            else:
                result = {"id": "msg_smoke", "type": "message", "role": "assistant", "model": body["model"],
                          "content": [{"type": "text", "text": "kiro-smoke-ok"}], "stop_reason": "end_turn",
                          "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 3}}
                if body.get("stream"):
                    events = [("message_start", {"type": "message_start", "message": {**result, "content": []}}),
                              ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                              ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "kiro-smoke-ok"}}),
                              ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                              ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}}),
                              ("message_stop", {"type": "message_stop"})]
                    payload = "".join(f"event: {event}\ndata: {json.dumps(value)}\n\n" for event, value in events).encode()
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(result).encode()
                    content_type = "application/json"
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    mock = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=mock.serve_forever, daemon=True)
    thread.start()
    proxy_port, cave_port = port(), port()
    processes = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    temporary_directory = tempfile.TemporaryDirectory(prefix="kiro-smoke-")
    try:
        with contextlib.nullcontext(temporary_directory.name) as temporary:
            directory = Path(temporary)
            accounts = directory / "accounts.json"
            accounts.write_text(json.dumps({"active": "mock", "accounts": {"mock": {
                "baseUrl": f"http://127.0.0.1:{mock.server_port}", "apiKey": "mock-gateway-secret"}}}))
            config = directory / "caveman.yaml"
            config.write_text(f"providers:\n  anthropic:\n    base_url: http://127.0.0.1:{proxy_port}\n")
            env = {**os.environ, "CCP_CONFIG_DIR": str(directory / "ccp"), "CCP_KIRO_ACCOUNTS_FILE": str(accounts),
                   "CCP_ALIAS_PROVIDER": "kiro", "CCP_BIND_ADDRESS": "127.0.0.1", "PORT": str(proxy_port),
                   "CAVEMAN_HOME": str(directory / "caveman"), "CAVEMAN_DB": str(directory / "caveman.db"),
                   "CAVEMAN_CONFIG": str(config), "CAVEMAN_LISTEN": f"127.0.0.1:{cave_port}", "CAVEMAN_MODE": "record"}
            env["CAVE_SSRF_ALLOWLIST"] = f"127.0.0.1:{proxy_port}"
            env["CCP_STATE_DIR"] = str(directory / "ccp-state")
            binary = ROOT / "target/release" / ("claude-code-proxy.exe" if os.name == "nt" else "claude-code-proxy")
            with (directory / "services.log").open("wb") as log:
                processes.append(subprocess.Popen([str(binary), "serve", "--no-monitor"], env=env, stdout=log, stderr=log))
                processes.append(subprocess.Popen([args.caveman_bin], env=env, stdout=log, stderr=log))
                deadline = time.monotonic() + 15
                for service_port in (proxy_port, cave_port):
                    while True:
                        try:
                            with socket.create_connection(("127.0.0.1", service_port), timeout=0.2):
                                break
                        except OSError:
                            if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
                                raise RuntimeError("Service failed to start: " + (directory / "services.log").read_text())
                            time.sleep(0.1)
                request = {"model": "kiro:claude-sonnet-4-5", "max_tokens": 50,
                           "messages": [{"role": "user", "content": "smoke input"}],
                           "tools": [{"name": "read_file", "description": "Read a file", "input_schema": {"type": "object", "properties": {}}}]}
                for endpoint, stream in [("messages", False), ("messages", True), ("messages/count_tokens", False)]:
                    body = {**request, "stream": stream}
                    outgoing = urllib.request.Request(f"http://127.0.0.1:{cave_port}/w/claude/v1/{endpoint}",
                        data=json.dumps(body).encode(), headers={"content-type": "application/json", "x-api-key": "unused", "anthropic-version": "2023-06-01"})
                    try:
                        with opener.open(outgoing, timeout=15) as response:
                            output = response.read().decode()
                    except urllib.error.HTTPError as error:
                        raise RuntimeError(f"HTTP {error.code}: {error.read().decode()}\n" + (directory / "services.log").read_text()) from None
                    assert "kiro-smoke-ok" in output if endpoint == "messages" else json.loads(output)["input_tokens"] == 12
                assert len(captured) == 3, captured
                for _path, headers, payload in captured:
                    assert headers.get("x-api-key") == "mock-gateway-secret", headers
                    assert payload["model"] == "claude-sonnet-4-5", payload
                    assert payload["messages"] == request["messages"], payload
                    assert payload["tools"] == request["tools"], payload
                print("PASS: installed Caveman -> main Claude proxy -> mock Kiro: JSON, SSE, token counting and tool payloads")
                for process in reversed(processes):
                    process.terminate()
                    process.wait(timeout=10)
                processes.clear()
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        mock.shutdown()
        mock.server_close()
        temporary_directory.cleanup()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run Claude Code Proxy with isolated, manually selected Kiro accounts.

Only Python's standard library is needed for this controller. Gateway dependencies
live in the repository's .venv. Credentials and logs live outside the checkout.
"""
import argparse
import contextlib
import csv
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "vendor" / "kiro-gateway"
PROXY_PORT = 18765
CAVE_PORT = 18764
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")
_secured = set()


def home():
    return Path(os.environ.get("CCP_KIRO_HOME", Path.home() / ".claude-kiro-proxy")).expanduser().resolve()


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Private state directory cannot be a symbolic link")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path in _secured:
        return path
    if os.name == "nt":
        raw = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
        sid = next(csv.reader([raw.strip()]))[1]
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        path.chmod(0o700)
    _secured.add(path)
    return path


def state_dir():
    path = home()
    if path in (Path.home().resolve(), ROOT, Path(path.anchor)):
        raise ValueError("CCP_KIRO_HOME must name a dedicated private directory")
    return private_dir(path)


@contextlib.contextmanager
def locked(name, wait=True):
    """OS locks survive crashes without stale-lock or PID-reuse problems."""
    path = state_dir() / name
    with path.open("a+b") as stream:
        # Windows byte-range locks also block reads. Acquire before any I/O;
        # msvcrt permits a lock extending beyond the end of an empty file.
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not wait:
                        raise RuntimeError("Another stack operation is already running") from None
                    time.sleep(0.1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                raise RuntimeError("Another stack operation is already running") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_write(path, content):
    """Publish complete private files, never a partly written snapshot."""
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    atomic_write(path, json.dumps(value, indent=2) + "\n")


def read_accounts():
    path = state_dir() / "kiro-accounts.json"
    if not path.exists():
        return {"active": None, "accounts": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("accounts"), dict):
        raise ValueError("Invalid account registry; restore it from your backup")
    for name, account in value["accounts"].items():
        if not NAME.fullmatch(name) or not isinstance(account, dict):
            raise ValueError("Invalid saved account")
        port = account.get("port")
        if not isinstance(port, int) or not 18770 <= port <= 65535:
            raise ValueError("Invalid gateway port")
        if account.get("baseUrl") != f"http://127.0.0.1:{port}" or not account.get("apiKey"):
            raise ValueError("Invalid local gateway connection")
        if account.get("kind") not in ("json", "sqlite"):
            raise ValueError("Invalid credential type")
        expected = state_dir() / "accounts" / name / ("token.json" if account["kind"] == "json" else "data.sqlite3")
        if Path(account.get("credentialsFile", "")).resolve() != expected:
            raise ValueError("Credential path must stay inside its account directory")
    if value.get("active") is not None and value["active"] not in value["accounts"]:
        raise ValueError("Selected account does not exist")
    return value


def saved_credentials(source):
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("refreshToken"), str) or not data["refreshToken"]:
        raise ValueError("Credentials must contain a refreshToken; sign in through Kiro first")
    client_hash = data.get("clientIdHash")
    if client_hash and not (data.get("clientId") and data.get("clientSecret")):
        if not isinstance(client_hash, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", client_hash):
            raise ValueError("Invalid device registration identifier")
        candidates = [source.parent / f"{client_hash}.json", Path.home() / ".aws/sso/cache" / f"{client_hash}.json"]
        registration = next((p for p in candidates if p.is_file()), None)
        if registration is None:
            raise ValueError("The matching AWS device registration file is missing")
        device = json.loads(registration.read_text(encoding="utf-8-sig"))
        for key in ("clientId", "clientSecret"):
            if not device.get(key):
                raise ValueError("Incomplete AWS device registration")
            data[key] = device[key]
    # Embed device registration so later Kiro sign-ins cannot change this account.
    data.pop("clientIdHash", None)
    return data


def add_account(name, source, kind="json"):
    if not NAME.fullmatch(name):
        raise ValueError("Account names use lowercase letters, digits, _ or - (maximum 40 characters)")
    source = Path(source).expanduser().resolve()
    data = saved_credentials(source) if kind == "json" else None
    if kind == "sqlite" and not source.is_file():
        raise ValueError("Kiro CLI database does not exist")
    with locked("accounts.lock"):
        registry = read_accounts()
        if name in registry["accounts"]:
            raise ValueError("Account already exists; use a new name to preserve its refreshed credentials")
        folder = private_dir(state_dir() / "accounts" / name)
        target = folder / ("token.json" if kind == "json" else "data.sqlite3")
        if kind == "json":
            atomic_json(target, data)
        else:
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as origin:
                with sqlite3.connect(target) as destination:
                    origin.backup(destination)
            if os.name != "nt":
                target.chmod(0o600)
        ports = {a["port"] for a in registry["accounts"].values()}
        port = next((p for p in range(18770, 65536) if p not in ports), None)
        if port is None:
            raise ValueError("No gateway ports available")
        registry["accounts"][name] = {
            "baseUrl": f"http://127.0.0.1:{port}", "apiKey": secrets.token_urlsafe(32),
            "port": port, "kind": kind, "credentialsFile": str(target),
        }
        if registry["active"] is None:
            registry["active"] = name
        atomic_json(state_dir() / "kiro-accounts.json", registry)
    print(f"Saved account {name}. Your Kiro installation's credentials were left in place.")


def use_account(name):
    with locked("accounts.lock"):
        registry = read_accounts()
        if name not in registry["accounts"]:
            raise ValueError("Unknown account; run account list")
        registry["active"] = name
        atomic_json(state_dir() / "kiro-accounts.json", registry)
    print(f"Selected {name} for new Kiro requests. Existing responses continue on their original account.")


def gateway_python():
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def proxy_binary():
    return ROOT / "target" / "release" / ("claude-code-proxy.exe" if os.name == "nt" else "claude-code-proxy")


def setup():
    if sys.version_info < (3, 12):
        raise RuntimeError("Setup requires Python 3.12 or newer")
    subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=ROOT, check=True)
    if not gateway_python().is_file():
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    subprocess.run([str(gateway_python()), "-m", "pip", "install", "-r", str(ROOT / "scripts/requirements-kiro.lock")], check=True)
    subprocess.run(["cargo", "build", "--release", "--locked"], cwd=ROOT, check=True)
    state_dir()
    print("Setup complete. Import each signed-in Kiro account with account add, then activate your shell.")


def http_json(url, key=None):
    # Local workers never use an HTTP_PROXY supplied by the environment.
    request = urllib.request.Request(url, headers={"x-api-key": key} if key else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=2) as response:
        return json.load(response)


def running():
    try:
        # Only a live OS lock proves ownership. A stale JSON marker cannot do so.
        with locked("service.lock", wait=False):
            return False
    except RuntimeError:
        try:
            info = json.loads((state_dir() / "service.json").read_text())
            return info.get("ready") is True and http_json(f"http://127.0.0.1:{PROXY_PORT}/healthz").get("ok") is True
        except (OSError, ValueError):
            return False


def service_owned():
    try:
        with locked("service.lock", wait=False):
            return False
    except RuntimeError:
        return True


def available(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"Port {port} is occupied. Stop the conflicting service before starting this stack.") from None


def start_child(command, env, log):
    with log.open("ab") as output:
        return subprocess.Popen(command, env=env, cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=output,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def stop_child(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def wait_child(child, url, key=None):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("A proxy exited during startup; inspect the private state directory's logs")
        try:
            http_json(url, key)
            return
        except (OSError, ValueError):
            time.sleep(0.25)
    raise RuntimeError("Proxy startup timed out; inspect the private state directory's logs")


def serve():
    if not gateway_python().is_file() or not proxy_binary().is_file():
        raise RuntimeError("Run scripts/stack.py setup first")
    children = []
    workers = {}
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with locked("service.lock", wait=False):
        directory = state_dir()
        marker = directory / "service.json"
        marker.unlink(missing_ok=True)
        (directory / "stop").unlink(missing_ok=True)
        try:
            available(PROXY_PORT)
            env = {**os.environ, "CCP_KIRO_ACCOUNTS_FILE": str(directory / "kiro-accounts.json"),
                   "CCP_STATE_DIR": str(directory / "main-state"),
                   "CCP_BIND_ADDRESS": "127.0.0.1", "PORT": str(PROXY_PORT), "CCP_ALIAS_PROVIDER": "kiro"}
            proxy = start_child([str(proxy_binary()), "serve", "--no-monitor"], env, directory / "proxy.log")
            children.append(proxy)
            wait_child(proxy, f"http://127.0.0.1:{PROXY_PORT}/healthz")
            print(f"Claude proxy: http://127.0.0.1:{PROXY_PORT}. Ctrl+C stops the stack.", flush=True)
            while not stopping and not (directory / "stop").exists():
                if proxy.poll() is not None:
                    raise RuntimeError("Claude proxy exited; inspect proxy.log")
                registry = read_accounts()
                for name, account in registry["accounts"].items():
                    if name in workers:
                        continue
                    folder = directory / "accounts" / name
                    available(account["port"])
                    atomic_json(folder / "gateway-accounts.json", [{"type": account["kind"], "path": account["credentialsFile"]}])
                    # Never inherit another account, auto-failover, or debug capture.
                    worker_env = {k: v for k, v in os.environ.items()
                                  if not k.startswith(("KIRO_", "ACCOUNTS_")) and k not in
                                  ("REFRESH_TOKEN", "PROFILE_ARN", "PROXY_API_KEY", "ACCOUNT_SYSTEM", "DEBUG_MODE")}
                    worker_env.update({"PROXY_API_KEY": account["apiKey"], "SERVER_HOST": "127.0.0.1",
                                       "SERVER_PORT": str(account["port"]), "ACCOUNT_SYSTEM": "false", "DEBUG_MODE": "off",
                                       "ACCOUNTS_CONFIG_FILE": str(folder / "gateway-accounts.json"),
                                       "ACCOUNTS_STATE_FILE": str(folder / "gateway-state.json")})
                    worker = start_child([str(gateway_python()), str(GATEWAY / "main.py"),
                                          "--host", "127.0.0.1", "--port", str(account["port"])], worker_env, folder / "gateway.log")
                    children.append(worker)
                    workers[name] = worker
                    # One bad account must not prevent other providers or accounts working.
                    print(f"Starting Kiro account {name}; log: {folder / 'gateway.log'}", flush=True)
                if not marker.exists():
                    atomic_json(marker, {"ready": True, "pid": os.getpid()})
                    open_monitor()
                time.sleep(0.5)
        finally:
            marker.unlink(missing_ok=True)
            for child in reversed(children):
                stop_child(child)
            (directory / "stop").unlink(missing_ok=True)


def ensure_running():
    if running():
        return
    directory = state_dir()
    env = os.environ.copy()
    env["CCP_KIRO_HOME"] = str(directory)
    with (directory / "stack.log").open("ab") as output:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"], env=env,
                         stdin=subprocess.DEVNULL, stdout=output, stderr=output, cwd=ROOT,
                         start_new_session=os.name != "nt",
                         creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if running():
            return
        if child.poll() is not None and not service_owned():
            raise RuntimeError(f"Stack exited during startup; inspect {directory / 'stack.log'}")
        time.sleep(0.25)
    raise RuntimeError(f"Stack did not start; inspect {directory / 'stack.log'}")


def launch_env():
    directory = state_dir()
    config = directory / "caveman.yaml"
    content = f"providers:\n  anthropic:\n    base_url: http://127.0.0.1:{PROXY_PORT}\n"
    if not config.exists() or config.read_text(encoding="utf-8") != content:
        # Serialize configuration writers with account operations; write once.
        with locked("accounts.lock"):
            atomic_write(config, content)
    allowlist = os.environ.get("CAVE_SSRF_ALLOWLIST", "").strip(", ")
    allowlist = ",".join(filter(None, [allowlist, f"127.0.0.1:{PROXY_PORT}"]))
    env = {**os.environ, "CAVEMAN_CONFIG": str(config), "CAVE_GATEWAY_URL": f"http://127.0.0.1:{CAVE_PORT}",
            "CAVE_SSRF_ALLOWLIST": allowlist,
            "CAVEMAN_LISTEN": f"127.0.0.1:{CAVE_PORT}", "ANTHROPIC_AUTH_TOKEN": "unused",
            "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", "kiro:claude-sonnet-4-5"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    # Alias routing belongs to the main proxy and its session affinity. Explicit
    # per-alias overrides would force auxiliary calls back to Kiro after /model gpt-*.
    return env


def requested_model(arguments):
    for index, argument in enumerate(arguments):
        if argument == "--model" and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith("--model="):
            return argument.split("=", 1)[1]
    return os.environ.get("ANTHROPIC_MODEL", "kiro:claude-sonnet-4-5")


def monitor():
    """Attach the upstream dashboard; its lifetime is separate from the service."""
    with locked("monitor.lock", wait=False):
        ensure_running()
        return subprocess.call([str(proxy_binary()), "monitor", "--url", f"http://127.0.0.1:{PROXY_PORT}"])


def open_monitor():
    """Interactive Windows sessions show the original dashboard in its own console."""
    if sys.platform != "win32" or not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    try:
        with locked("monitor.lock", wait=False):
            pass
    except RuntimeError:
        return
    env = {**os.environ, "CCP_KIRO_HOME": str(state_dir())}
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "monitor"],
                         env=env, cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError as error:
        print(f"Could not open the dashboard: {error}. Run scripts/stack.py monitor in another terminal.", file=sys.stderr)


def run_claude(arguments):
    registry = read_accounts()
    model = requested_model(arguments)
    uses_kiro = model.startswith(("kiro:", "claude-")) or model in ("sonnet", "haiku", "opus")
    if uses_kiro and registry["active"] is None:
        raise RuntimeError("No Kiro account saved. Sign in to Kiro and run account add first, or pass --model gpt-6-sol.")
    ensure_running()
    # Explicit non-Kiro models can run without any Kiro credentials.
    if uses_kiro and registry["active"] is not None:
        account = registry["accounts"][registry["active"]]
        deadline = time.monotonic() + 120
        while True:
            try:
                http_json(account["baseUrl"] + "/v1/models", account["apiKey"])
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Selected Kiro account is not ready; inspect its gateway.log or select another account") from None
                time.sleep(0.5)
    executable = os.environ.get("CCP_CAVEMAN_BIN") or shutil.which("caveman.cmd" if os.name == "nt" else "caveman")
    if not executable:
        raise RuntimeError("Caveman is not installed or not on PATH")
    # .cmd launchers need cmd.exe; list2cmdline alone cannot escape shell syntax.
    if os.name == "nt" and Path(executable).suffix.lower() == ".cmd":
        script = Path(executable).parent / "node_modules/@caveman-ai/cli/dist/index.js"
        if not script.is_file():
            raise RuntimeError("Set CCP_CAVEMAN_BIN to the Caveman executable, or install @caveman-ai/cli globally")
        command = [shutil.which("node") or "node", str(script), "claude", *arguments]
    else:
        command = [executable, "claude", *arguments]
    open_monitor()
    return subprocess.call(command, env=launch_env())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Install isolated gateway dependencies and build the main proxy")
    commands.add_parser("serve", help="Run the main proxy and one gateway per saved account")
    commands.add_parser("monitor", help="Open the original Claude proxy dashboard without stopping its services on exit")
    commands.add_parser("stop", help="Stop this stack's supervised processes")
    commands.add_parser("status", help="Show local service and account status without credentials")
    run = commands.add_parser("run", help="Auto-start the stack and forward arguments to caveman claude")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    accounts = commands.add_parser("account", help="Manually manage saved paid Kiro accounts").add_subparsers(dest="action", required=True)
    add = accounts.add_parser("add")
    add.add_argument("name")
    credentials = add.add_mutually_exclusive_group()
    credentials.add_argument("--creds", default=str(Path.home() / ".aws/sso/cache/kiro-auth-token.json"))
    credentials.add_argument("--sqlite")
    accounts.add_parser("list")
    use = accounts.add_parser("use")
    use.add_argument("name")
    args = parser.parse_args(argv)
    if args.command == "setup":
        setup()
    elif args.command == "serve":
        serve()
    elif args.command == "monitor":
        return monitor()
    elif args.command == "stop":
        if service_owned():
            (state_dir() / "stop").touch()
            print("Stop requested. The supervisor will stop only its own child processes.")
        else:
            print("Stack is stopped or still starting.")
    elif args.command == "run":
        return run_claude(args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments)
    elif args.command == "account" and args.action == "add":
        add_account(args.name, args.sqlite or args.creds, "sqlite" if args.sqlite else "json")
    elif args.command == "account" and args.action == "use":
        use_account(args.name)
    else:
        if args.command == "status":
            print("Stack is running" if running() else "Stack is stopped or starting")
        registry = read_accounts()
        if not registry["accounts"]:
            print("No Kiro accounts saved. Run account add after signing in to Kiro.")
        for name, account in registry["accounts"].items():
            print(f"{'*' if name == registry['active'] else ' '} {name} ({account['kind']})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(f"stack: {error}", file=sys.stderr)
        sys.exit(1)

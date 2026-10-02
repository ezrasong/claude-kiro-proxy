# Claude + Kiro Proxy

[Claude Code Proxy](https://github.com/raine/claude-code-proxy) as the main server,
with [Kiro Gateway](https://github.com/jwadow/kiro-gateway) as an added provider,
Caveman in front, and a manual swapper for your paid Kiro accounts.

```powershell
python scripts/stack.py setup
# Sign in through Kiro, then save that account:
python scripts/stack.py account add paid-a
./scripts/install.ps1

caveman claude --model kiro:claude-sonnet-4-5
caveman claude --model gpt-6-sol
```

Both backends stay available. Claude arguments pass through unchanged.
The launcher automatically starts the services when needed.
The Windows installer activates this terminal and new PowerShell terminals.
For temporary activation only, use `. ./scripts/activate.ps1` instead.
On Windows, interactive launches also open the original Claude proxy dashboard
in a separate terminal. Closing the dashboard leaves the services running.
Open it manually with `python scripts/stack.py monitor`.

```powershell
# Sign in to another paid account through Kiro, then:
kiro-account add paid-b
kiro-account list
kiro-account use paid-b
```

Swaps affect new Kiro requests. Existing streams finish on their original account.
Accounts do not rotate automatically. Saved credentials stay outside the repo.
Codex models use the main proxy's existing login:
`./target/release/claude-code-proxy.exe codex auth login`.

Requirements: Git, Python 3.12+, Rust with platform build tools, Claude Code,
Caveman (including its local proxy), and a signed-in Kiro IDE or CLI.
Clone with `git clone --recurse-submodules` before running setup.

**[Full setup, account management, model routing and troubleshooting](docs/KIRO.md)**

macOS/Linux: use `python3` and `source scripts/activate.sh` in Bash.

This repo retains the upstream Claude proxy's code and history (MIT).
Kiro Gateway is a pinned, unmodified submodule and separate HTTP service
(AGPL-3.0). Both projects' source and license notices are preserved.
See [the upstream README](docs/UPSTREAM-README.md) for the original providers.

Integration tests use local mock gateways; a live paid-account request is a
separate verification step after you import your credentials.

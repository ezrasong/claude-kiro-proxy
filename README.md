# Claude + Kiro Proxy

[Claude Code Proxy](https://github.com/raine/claude-code-proxy) as the main server,
with [Kiro Gateway](https://github.com/jwadow/kiro-gateway) as an added provider,
Caveman in front, and a manual swapper for your paid Kiro accounts.

```powershell
python scripts/stack.py setup
# Sign in through Kiro CLI, then save that account:
kiro-cli login
python scripts/stack.py account add paid-a
./scripts/install.ps1

caveman claude --model kiro:claude-sonnet-4-5
caveman claude --model gpt-6-sol
```

Both backends stay available. Claude arguments pass through; supplied `--settings`
are merged with the session's local proxy route without changing your settings files.
The launcher automatically starts the services when needed.
The Windows installer activates this terminal and new PowerShell terminals.
For temporary activation only, use `. ./scripts/activate.ps1` instead.
On Windows, interactive launches also open the original Claude proxy dashboard
in a separate terminal. Closing the dashboard leaves the services running.
Open it manually with `python scripts/stack.py monitor`.

```powershell
# Sign in to another paid account through Kiro CLI, then:
kiro-cli logout
kiro-cli login
kiro-account add paid-b
kiro-account list
kiro-account use paid-b
# Delete a saved account and its proxy credentials/logs:
kiro-account delete paid-a
```

Swaps affect new Kiro requests. Existing streams finish on their original account.
Accounts do not rotate automatically. Saved credentials stay outside the repo.
Imports prefer the installed Kiro CLI store and reject duplicate credentials.
For Kiro IDE accounts, pass `--creds` with the IDE token JSON path.
Deleting an account stops its gateway and interrupts its active responses.
Deleting the active account clears selection; use `kiro-account use <name>` to
select another one. Chrome profiles and the Kiro installation login are preserved.
Codex models use the main proxy's existing login:
`./target/release/claude-code-proxy.exe codex auth login`.

Requirements: Git, Python 3.12+, Rust with platform build tools, Claude Code,
Caveman (including its local proxy), and a signed-in Kiro IDE or CLI.
Clone with `git clone --recurse-submodules` before running setup.

**[Full setup, account management, model routing and troubleshooting](docs/KIRO.md)**

macOS/Linux: use `python3` and `source scripts/activate.sh` in Bash.

This repo retains the upstream Claude proxy's code and history (MIT).
Kiro Gateway is a pinned submodule and separate HTTP service (AGPL-3.0), with a
tracked patch that reports service refusals instead of empty responses. Setup
applies the patch automatically. Its synthetic reasoning injection is disabled.
Both projects' source and license notices are preserved.
See [the upstream README](docs/UPSTREAM-README.md) for the original providers.

Integration tests use local mock gateways; a live paid-account request is a
separate verification step after you import your credentials.

# Kiro integration guide

Claude Code Proxy is the main server. Kiro Gateway is an additional provider,
with manually selected, separately saved Kiro accounts. Caveman stays in front
of both. Your usual Claude Code arguments are forwarded unchanged.

```text
caveman claude [arguments]
  → Caveman local proxy (127.0.0.1:18764)
  → Claude Code Proxy   (127.0.0.1:18765)
      ├─ gpt-* / kimi-* / grok-* / cursor:* → existing providers
      └─ kiro:<model> → selected account's Kiro Gateway → Kiro
```

Both providers run at once. Choose models per session or change them with
`/model`. A single request uses one provider; responses are not combined.
Selecting another Kiro account affects new requests immediately and leaves
already-running streams on their original account. Selection is global to
this stack's Kiro sessions. There is no automatic account rotation or failover.

## Setup

Requirements: Git, **Python 3.12 or newer**, Rust with platform build tools,
Claude Code, Caveman, and Kiro IDE or CLI. On Windows, Rust needs MSVC build
tools. Sign in to your paid account through Kiro itself. This controller imports
credentials; it does not collect passwords or implement a new sign-in flow.

```powershell
git clone --recurse-submodules https://github.com/ezrasong/claude-kiro-proxy.git
cd claude-kiro-proxy
python scripts/stack.py setup
python scripts/stack.py account add paid-a
./scripts/install.ps1
caveman claude --model kiro:claude-sonnet-4-5
```

Setup installs pinned gateway dependencies in `.venv` and builds this fork's
Rust binary. It does not install Caveman, Claude Code, or Kiro. If Caveman is
missing, install it with `npm install -g @caveman-ai/cli` and follow its setup
instructions for the local proxy binary. This launcher uses your installed
Caveman and preserves its compression controls.

The launcher starts the main proxy and gateway workers in the background when
needed. It does not alter global Claude settings or Caveman's installed package.
Interactive Windows launches open the original Claude proxy terminal dashboard
in a separate console. The dashboard can stay open across Claude sessions;
closing it leaves the background proxies running. Noninteractive runs do not
open a window. On other platforms, open the dashboard in another terminal with
`python3 scripts/stack.py monitor`.
The Windows installer preserves and backs up existing profiles, then adds the
activation line to current-user Windows PowerShell and PowerShell 7 profiles.
New terminals automatically use the launcher. For temporary activation only,
dot-source `scripts/activate.ps1` once per terminal. Only `caveman claude` enters
this stack; other Caveman commands use
your existing installation. Closing the terminal removes the functions; remove
the profile activation line to undo persistent activation.

Claude Code does not need an Anthropic subscription login for this local route.
The launcher supplies a local placeholder credential; real authentication belongs
to the selected Kiro account or the main proxy's other providers. It supplies a
private temporary `--settings` overlay so existing Claude settings cannot route
the session to an older gateway. Your own `--settings` are preserved except for
the local route and placeholder credential; original files are left in place.
Builder ID credentials without a profile ARN use the same shared service profile
as Kiro IDE. Explicit account profile ARNs are preserved. If Claude Code
asks you to sign in, ensure the launcher is active in that terminal. Verify the
actual launch path without making a model request:

```powershell
caveman claude --model kiro:claude-sonnet-4-5 auth status
```

This should report `loggedIn: true` for the local credential. It does not mean
you signed into an Anthropic subscription.

For Codex models, authenticate through the main proxy once:

```powershell
./target/release/claude-code-proxy.exe codex auth login
caveman claude --model gpt-6-sol
```

Existing Kimi, Grok, OpenCode and Cursor authentication continues to work. See
[the upstream README](UPSTREAM-README.md) and
[upstream documentation](https://claude-code-proxy.raine.dev).

## Save and swap paid accounts

Save each account **while it is signed in through Kiro**. Import a given account
once, under its own name. Names are local labels; models and subscription access
come from the imported account, not from its name.

```powershell
# Sign in to your first paid account in Kiro CLI:
kiro-cli login
kiro-account add paid-a

# Sign out and sign in to the second account in Kiro CLI:
kiro-cli logout
kiro-cli login
kiro-account add paid-b

kiro-account list
kiro-account use paid-b
caveman claude --model kiro:claude-sonnet-4-5

# Swap back while other sessions are open:
kiro-account use paid-a
```

The first imported account becomes active. Later imports do not change selection.
Rate-limit and quota errors remain attached to the selected account until you
manually select another one.

Remove a saved account with `kiro-account delete <name>`. This stops only its
gateway and deletes its saved proxy credentials, gateway state, and logs. Active
responses on that account are interrupted. Chrome profiles and the source Kiro
login are preserved. Deleting the active account clears selection; select another
one explicitly with `kiro-account use <name>`. Once deletion completes, its name
can be imported again. A pending deletion reserves the name and port until its
worker has stopped and its saved files have been removed; retry if cleanup was
blocked by file permissions.

Other credential locations and Kiro CLI databases are supported:

```powershell
kiro-account add work --creds 'C:/path/to/kiro-auth-token.json'
kiro-account add cli --sqlite 'C:/path/to/data.sqlite3'
```

The default source is the Kiro CLI store when present: `%LOCALAPPDATA%/Kiro-Cli/data.sqlite3`
on Windows and `$XDG_DATA_HOME/kiro-cli/data.sqlite3` (default `~/.local/share`) on
macOS/Linux. A logged-out CLI store produces an error instead of silently importing
an IDE login. Use `--creds ~/.aws/sso/cache/kiro-auth-token.json` explicitly for the
IDE; this is also the fallback when no CLI store exists. JSON imports include
matching AWS device registration for enterprise/SSO accounts. SQLite imports read
the login in one transaction and export only authentication fields into a private
gateway JSON snapshot; CLI conversations and history are not copied. Identical
refresh credentials cannot be imported under a second name. Saved copies receive
refreshed tokens; changing the CLI or IDE login does not overwrite them.
If an imported login is revoked or cannot refresh, sign in again and import it
under a new name, then select that name. Stop the stack before editing or restoring
saved credentials yourself.

Without activation, use `python scripts/stack.py account ...`, and launch with
`python scripts/stack.py run -- --model kiro:claude-sonnet-4-5`.

## Models

| Selection | Destination |
| --- | --- |
| `kiro:claude-sonnet-4-5` | Active Kiro account |
| `kiro:claude-haiku-4-5` | Active Kiro account |
| `kiro:claude-opus-4-5` | Active Kiro account, if it has access |
| `kiro:<another-model-id>` | Active account; gateway resolves its model catalog |
| `gpt-6-sol`, other registered `gpt-*` | Existing Codex provider |
| `kimi-for-coding`, `grok-*`, `cursor:*` | Existing Claude proxy providers |

Advertised Kiro models are examples, not guarantees of account availability. Use
the native Kiro IDE/CLI model list to inspect your account's actual catalog. The
`kiro:` prefix is stripped before forwarding.

The managed server defaults shorthand names (`sonnet`, `haiku`, `opus`) to Kiro.
After an explicit `gpt-*` or `kimi-*` request in the same Claude session, existing
session affinity routes auxiliary shorthand calls to that provider. Explicit
`kiro:` calls always reach Kiro. Direct use of the Rust binary preserves upstream's
Codex default; set `CCP_ALIAS_PROVIDER=kiro` for the managed stack's default.

Kiro's native Anthropic Messages API handles tools, images, thinking, streaming
and token counting. Translation stays in Kiro Gateway. Stream parsing, usage
monitoring and malformed-stream handling share the main proxy's existing native
Anthropic passthrough implementation. HTTP errors preserve status and `Retry-After`
without exposing gateway error bodies or credentials.

## Task completion

`caveman claude` adds Claude Code's native prompt-based `Stop` hook through its
private per-launch settings overlay. It reviews the requested scope and transcript
evidence, including relevant verification. Plans, partial progress and offers to
continue do not count as completion. When work remains, its feedback sends Claude
back into the same session to take the next concrete step. Questions and status
requests are evaluated against their own scope.

This adds a model evaluation when Claude tries to stop, with additional latency
and model usage. The evaluator uses Claude Code's model selection and the session's
proxy route. It is a best-effort evidence check, not a guarantee that
arbitrary tasks can be completed. Missing access or required input, upstream
refusals, exhausted limits and user cancellation remain valid stopping reasons.
The checker cannot run tools or grant permissions; existing hooks and permissions
are retained, including `disableAllHooks` if you have set it.

Claude Code's native limits still apply: the installed version allows eight
consecutive automatic continuations before ending the turn with a warning on
the ninth rejected stop. Explicit `--max-turns` and budget limits still apply.
Checker errors or timeouts follow Claude Code's hook error behavior. These cases
do not establish that the requested task is complete.

Press Ctrl+C to interrupt. The hook is added on launch, so an already-open Claude
session needs to be exited and resumed from the same project directory:

```powershell
caveman claude --continue --model kiro:claude-sonnet-4-5
```

Global and project settings files are not modified. The temporary overlay is
removed when the launcher exits, and account switching stays manual.

Claude Code does not need a Claude subscription login for this local route.
The launcher supplies the local placeholder token and proxy URL in both the
startup environment and its settings overlay. It also clears Caveman's saved
first-party endpoint assertion for this invocation, because Kiro is a custom
upstream. Authentication to Kiro remains with the saved account gateway.
If an existing PowerShell terminal is using the original Caveman command,
reload the launcher before retrying:

```powershell
. ./scripts/activate.ps1  # run from this repository
caveman claude --model kiro:claude-opus-5-5
```

From another directory you can invoke `scripts/stack.py run -- ...` by its full
path to avoid depending on shell activation. Claude Code's "API Usage Billing"
banner describes its API transport; it is not a request to log into Claude.

## Slow Opus coding turns

Long histories can make Kiro tool turns slow even when the HTTP response headers
arrive quickly. The monitor's completed request latency includes the streamed
generation; the structured log's `request_completed.ms` measures only the wait
for upstream headers. Use the monitor when comparing response times.

In the existing Claude session, `/compact` asks Claude to summarize the history
and retain task context. On Claude Code versions supporting it, `/autocompact 100k`
sets a smaller window for future native automatic summaries. This is a context
size tradeoff, not a model change: Opus stays selected, but detailed old tool
output is replaced by Claude's summary. `/autocompact auto` restores its native
window policy. Explicit environment overrides and disabled automatic compaction
still follow Claude Code's own precedence. The launcher does not force a window
or truncate messages in the proxy.

For a per-launch window instead of a saved Claude setting:

```powershell
caveman claude --continue --model kiro:claude-opus-5-5 --settings '{"autoCompactWindow":100000}'
```

Kiro's buffered endpoint returns headers only after generation finishes. The
adapter allows up to 330 seconds for headers and local gateway read inactivity,
giving the gateway's 300-second Kiro read deadline time to report an error.
Its former one-minute header cutoff could cause a healthy long
generation to fail and be repeated by the client. These limits remain bounded;
the adapter does not retry or switch accounts. This fix avoids premature retries,
but does not accelerate Kiro's model generation itself.

An Opus 5.5 diagnostic replay with roughly 130K input tokens took 120 seconds
through Caveman and the main proxy, and 261 seconds directly through the selected
gateway. A short-answer replay of the long history took 5.9 and 6.8 seconds,
respectively. These individual samples show that bypassing the stack did not
remove the delay in that comparison; they are not a throughput guarantee.

## Services and troubleshooting

```powershell
python scripts/stack.py status
python scripts/stack.py serve   # foreground; Ctrl+C stops owned processes
python scripts/stack.py stop    # stop the supervised main proxy and gateways
python scripts/stack.py monitor # original dashboard; starts the stack if needed
```

To install a changed Windows proxy binary, finish or exit the Claude session,
close the dashboard window, stop the stack, and run `python scripts/stack.py setup`
after the supervisor has stopped. Windows cannot replace an executable still in
use. Relaunch with `caveman claude --continue --model kiro:claude-opus-5-5` from the
same project directory to resume. Building source alone does not update a running
service.

State lives in `~/.claude-kiro-proxy`, outside the checkout. Set `CCP_KIRO_HOME`
before importing accounts to use another dedicated directory. The controller
applies user-only Windows ACLs or Unix directory/file permissions. Credentials,
gateway keys, refreshed tokens and logs must stay private. Each account has its
own gateway credentials configuration and runtime state. Debug payload capture
is disabled in the gateway workers.

`main-state/proxy.log` records the main proxy's structured logs, `proxy.log` its
console output, `stack.log` the background supervisor, and
`accounts/<name>/gateway.log` each gateway. Invalid credentials can prevent that
worker becoming ready. Select another account or import a fresh login. Codex
sessions do not wait for Kiro readiness.

Kiro can pause while reasoning or preparing tool calls. The gateway and main proxy send
Anthropic SSE `ping` events every 15 seconds during silence between complete
frames, keeping both local HTTP connections active through Caveman. A gateway
ping does not cancel the pending Kiro read or start a second request. Upstream
timeouts and stream validation still apply; keepalives do not retry requests or
switch accounts.

The launcher disables Claude Code's automatic non-streaming fallback, which can
repeat a stalled streamed generation and duplicate tool calls. Claude's ordinary
API retry limits still apply. An empty Kiro response with no content or tools is
an API error, rather than successful task completion. Stream failures remain
failed in the dashboard even after the HTTP body closes. Restart Claude with
`--continue` after a launcher update to load its new session environment.

The gateway's synthetic reasoning feature is disabled (`FAKE_REASONING=false`).
It adds instructions asking for exposed thinking, which can cause Kiro to refuse
ordinary Claude Code conversations with `CONTENT_FILTERED / REASONING_EXTRACTION`.
Effort settings do not provide native extended-thinking support through this gateway.
The proxy preserves tool results and user text without adding continuation prompts.

Setup and service startup apply `patches/kiro-refusals.patch` and
`patches/kiro-streaming.patch` to the pinned gateway. The refusal patch surfaces
Kiro refusal metadata as a visible explanation and a `refusal`
stop reason (OpenAI: `content_filter`), instead of an empty success. The gateway,
main proxy and completion checker do not retry refusals or route them to another
account. Claude Code may make its own built-in recovery attempt before reporting
a refusal. The service's suggested options are to
select another model, start a new conversation, or rewind to an earlier point.

Listeners bind to loopback. The main proxy retains upstream's local-client trust
model; other programs on your machine can call it. The Kiro adapter only accepts
literal loopback gateway URLs, bypasses environment HTTP proxies, and rejects
redirects so keys stay local. Occupied ports produce an error instead of attaching
to an unknown service. Do not expose these listeners to a network.

The dedicated Caveman route is `127.0.0.1:18764`, leaving an existing proxy on
`8787` untouched. Its YAML changes only the Anthropic upstream to the main proxy.
Caveman's mode and compression controls still apply. The launcher adds only
`127.0.0.1:18765` to Caveman's SSRF allowlist for the local upstream connection.
`stack.py stop` stops the
main proxy and gateways; Caveman keeps its own idle lifecycle.

## macOS / Linux

Use the setup and account commands with `python3`, then:

```bash
source scripts/activate.sh
caveman claude --model kiro:claude-sonnet-4-5
kiro-account use paid-b
```

The activation file targets Bash. Other shells can call `python3 scripts/stack.py
run -- ...` directly.

## Development

```text
cargo fmt --all -- --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --all --locked
python -m unittest discover -s tests -p test_stack.py
```

Tests use local mock gateways and cover routing, native payload preservation,
live account swapping, token counting, rate-limit behavior, safe errors, loopback
validation, independent imports, AWS device registration, atomic selection,
process locking and argument forwarding. No paid credentials are needed.

To exercise your installed Caveman proxy and the built Rust binary together
against a local mock Kiro gateway (no paid calls), run:

```powershell
python tests/smoke_caveman.py --caveman-bin "$HOME/.caveman/bin/caveman-proxy.exe"
```

To verify the installed Claude Code continuation loop without paid calls:

```powershell
python tests/smoke_completion.py --claude-bin "$HOME/.local/bin/claude.exe"
```

This deliberately simulates an early stop, checks the subsequent permitted tool
call and completion, preserves existing Stop hooks from two settings scopes,
and covers completed work, blockers, refusals and the native no-progress limit.

Upstream source and history are retained. Claude Code Proxy is MIT licensed;
the Kiro Gateway submodule and its downstream patch are separately AGPL-3.0 licensed. This repo
uses it as a separate HTTP service and preserves its source and license. Its
commit and gateway dependencies are pinned; the tracked refusal patch is applied
idempotently and leaves the submodule working tree modified. Conflicting gateway
edits stop startup instead of being overwritten. Run integration tests before
changing either pin or the patch.

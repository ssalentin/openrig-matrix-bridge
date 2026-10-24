# 0.11.0 release runbook

Operator-executable steps for upgrading from 0.10.0 to 0.11.0, verifying the
result, and rolling back if needed. Every step is designed to be run without
inference — copy the commands and check the expected output.

Reference: [Release plan](RELEASE_PLAN.md) · [Issue #8](https://github.com/ssalentin/openrig-matrix-bridge/issues/8)

## Prerequisites

- A working 0.10.0 installation (wheel or editable).
- The Matrix access token exported as `MATRIX_ACCESS_TOKEN` in the shell that
  will run `doctor` and `run`.
- `pip` (or `uv pip`) available in the bridge's virtual environment.
- Read access to the state directory
  (default `~/.local/state/openrig-matrix-bridge`).

## 1 — Stop the running bridge

```bash
# Find the bridge process. There is no lock or pid-file guard — the process
# check is mandatory because two instances sharing one state directory will
# race state.json and the crypto store.
pgrep -af 'openrig-matrix-bridge'

# Stop it (use the PID from above).
kill <PID>

# Confirm it is gone.
pgrep -af 'openrig-matrix-bridge'
# Expected: no output.
```

> **No single-instance guard exists.** Do not start 0.11.0 until the old
> process has fully exited. Skipping this check risks double-processing events
> and corrupting the crypto store.

## 2 — Record the installed version and source artifact

`--version` prints only the Python package version, not a commit SHA.
Use `pip show` to identify the exact installed artifact:

```bash
.venv/bin/openrig-matrix-bridge --version
# Expected: 0.10.0

.venv/bin/pip show openrig-matrix-bridge
# Note the Version and Location lines.
```

## 3 — Back up state and config

Back up the state directory and config without exposing secrets. The backup
must be restorable for rollback (step 8) and crypto-store recovery.

```bash
STATE_DIR="${STATE_DIR:-$HOME/.local/state/openrig-matrix-bridge}"
CONFIG_PATH="${CONFIG_PATH:-$HOME/.config/openrig-matrix-bridge/config.yaml}"
BACKUP_DIR="$HOME/openrig-matrix-bridge-backup-$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"
cp -a "$STATE_DIR" "$BACKUP_DIR/state"
cp -a "$CONFIG_PATH" "$BACKUP_DIR/config.yaml"

# Verify the backup exists and is non-empty.
ls -la "$BACKUP_DIR/state/state.json"
ls -la "$BACKUP_DIR/config.yaml"

echo "Backup at: $BACKUP_DIR"
```

> Do not share or commit the backup. It contains the Matrix crypto store and
> may contain credentials in `human-delivery.env`.

## 4 — Install the 0.11.0 candidate

Install the exact wheel or sdist. Do not use an editable install for
production.

```bash
# From the built wheel:
.venv/bin/pip install openrig_matrix_bridge-0.11.0-py3-none-any.whl

# Or from sdist:
# .venv/bin/pip install openrig-matrix-bridge-0.11.0.tar.gz

# Confirm the installed version.
.venv/bin/openrig-matrix-bridge --version
# Expected: 0.11.0

.venv/bin/pip show openrig-matrix-bridge
# Record the Version and Installer lines for evidence.
```

## 5 — Run doctor

```bash
MATRIX_ACCESS_TOKEN='...' .venv/bin/openrig-matrix-bridge doctor \
  --config "$CONFIG_PATH"
```

All checks must pass. Stop and investigate any failure before starting the
bridge.

## 6 — Live attribution smoke tests

Start the bridge and verify that live attribution works for all three prompt
shapes. Each test must produce an answer delivered to the Matrix room, a thread
reply, and (where applicable) a closed qitem.

### 6a — Plain single-line prompt

1. Send a short plain-text message in the Matrix room.
2. Observe:
   - The message appears as a user message in the agent transcript.
   - An answer is delivered back to the Matrix room as a thread reply.
   - The corresponding qitem reaches a terminal state.

### 6b — Multi-line prompt

1. Send a message with at least two lines (e.g. a question with a code block).
2. Observe the same three outcomes as 6a.

### 6c — Attachment prompt with answer

1. Send a file attachment (e.g. a small PDF or text file) with a question.
2. Observe:
   - The file is delivered to the agent's inbox.
   - An answer referencing the file is delivered to Matrix.
   - The thread and qitem close normally.

### Recording evidence

For each test, record:

- The Matrix event ID of the sent message.
- The Matrix event ID of the answer reply.
- The qitem ID and its final state.
- A redacted summary (no private identifiers, homeserver URLs, tokens, or
  credentials).

> If any prompt shape fails attribution, the bridge degrades to
> `response_timeout_minutes` silence for every request — worse than 0.10.0.
> Do not proceed to production use; investigate and report.

## 7 — Restart verification

```bash
# Confirm only one bridge process.
pgrep -af 'openrig-matrix-bridge'
# Expected: exactly one PID.

# Restart (kill + start).
kill <PID>
MATRIX_ACCESS_TOKEN='...' .venv/bin/openrig-matrix-bridge run \
  --config "$CONFIG_PATH" &

# After restart, repeat one plain prompt (6a) and confirm the answer arrives.
```

## 8 — Rollback

If 0.11.0 is not acceptable, restore the pre-upgrade state. **Do not run
0.10.0 against 0.11.0 state** — the 0.11.0 schema adds fields (steering
groups) that 0.10.0 would misinterpret as plain prompts.

```bash
# 1. Stop the 0.11.0 bridge.
pgrep -af 'openrig-matrix-bridge'
kill <PID>
pgrep -af 'openrig-matrix-bridge'
# Expected: no output.

# 2. Restore the backup created in step 3.
STATE_DIR="${STATE_DIR:-$HOME/.local/state/openrig-matrix-bridge}"
CONFIG_PATH="${CONFIG_PATH:-$HOME/.config/openrig-matrix-bridge/config.yaml}"

rm -rf "$STATE_DIR"
cp -a "$BACKUP_DIR/state" "$STATE_DIR"
cp -a "$BACKUP_DIR/config.yaml" "$CONFIG_PATH"

# 3. Reinstall 0.10.0.
.venv/bin/pip install openrig_matrix_bridge-0.10.0-py3-none-any.whl

.venv/bin/openrig-matrix-bridge --version
# Expected: 0.10.0

# 4. Run doctor and start.
MATRIX_ACCESS_TOKEN='...' .venv/bin/openrig-matrix-bridge doctor \
  --config "$CONFIG_PATH"
MATRIX_ACCESS_TOKEN='...' .venv/bin/openrig-matrix-bridge run \
  --config "$CONFIG_PATH" &

# 5. Confirm one process and a working prompt.
pgrep -af 'openrig-matrix-bridge'
```

> The rollback restores the crypto store from the backup. Without it, old
> encrypted history cannot be decrypted. Never delete the crypto store.

## Evidence checklist

Before handing off, the operator records:

| Item | Value |
|---|---|
| Installed version (`--version`) | |
| `pip show` output (version, location) | |
| Doctor result | |
| Plain prompt: event IDs, qitem ID, state | |
| Multi-line prompt: event IDs, qitem ID, state | |
| Attachment prompt: event IDs, qitem ID, state | |
| Restart: post-restart prompt result | |
| Single-instance check (one PID) | |
| Candidate SHA | |

No tokens, MXC URIs, bearer headers, homeserver URLs, credentials, or
non-synthetic user IDs in recorded evidence.

# 0.11.0 release runbook

Operator-executable steps for upgrading from 0.10.0 to 0.11.0, verifying the
result, and rolling back if needed. Every step is designed to be run without
inference — copy the commands and check the expected output.

Reference: [Release plan](RELEASE_PLAN.md) · [Issue #8](https://github.com/ssalentin/openrig-matrix-bridge/issues/8)

## Paths and conventions

This runbook uses production paths. Adapt if your layout differs.

| Item | Production default |
|---|---|
| Virtual environment | `/opt/openrig-matrix/venv` |
| Config | `/etc/openrig-matrix-bridge/config.yaml` |
| State directory | `/root/.local/share/openrig-matrix-bridge` |
| systemd unit | `openrig-matrix-bridge.service` |
| Environment file | `/etc/openrig-matrix-bridge/matrix.env` |

The examples below use these paths directly. A local development install would
use `~/.local/state/openrig-matrix-bridge` for state and `.venv` for the
virtual environment — substitute accordingly.

## 1 — Stop the running bridge

```bash
sudo systemctl stop openrig-matrix-bridge.service

# Confirm the process is gone. There is no lock or pid-file guard — this check
# is mandatory because two instances sharing one state directory will race
# state.json and the crypto store.
pgrep -af 'openrig-matrix-bridge'
# Expected: no output.
```

> **No single-instance guard exists.** Do not start 0.11.0 until the old
> process has fully exited. Skipping this check risks double-processing events
> and corrupting the crypto store.

## 2 — Record the installed version and source artifact

`--version` prints only the Python package version, not a commit SHA. Use
`pip show` and the installed wheel hash to identify the exact source artifact:

```bash
/opt/openrig-matrix/venv/bin/openrig-matrix-bridge --version
# Expected: 0.10.0

/opt/openrig-matrix/venv/bin/pip show openrig-matrix-bridge
# Record Version and Location.

# Record the installed wheel file hash for comparison with the release asset.
WHEEL_PATH=$(/opt/openrig-matrix/venv/bin/pip show -f openrig-matrix-bridge \
  | sed -n 's/^Location: //p')
sha256sum "$WHEEL_PATH"/openrig_matrix_bridge-*.dist-info/RECORD 2>/dev/null \
  || echo "editable install — no wheel RECORD"
```

When verifying the 0.11.0 candidate (step 4), compare the installed wheel file
hash with the release asset digest published alongside the candidate SHA.
Record the expected source SHA separately in the evidence — `pip show` alone
does not prove the exact candidate source.

## 3 — Back up state and config

Back up the state directory and config without exposing secrets. The backup
must be restorable for rollback (step 9) and crypto-store recovery.

```bash
STATE_DIR="/root/.local/share/openrig-matrix-bridge"
CONFIG_PATH="/etc/openrig-matrix-bridge/config.yaml"
BACKUP_DIR="/root/openrig-matrix-bridge-backup-$(date -u +%Y%m%dT%H%M%SZ)"

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
/opt/openrig-matrix/venv/bin/pip install openrig_matrix_bridge-0.11.0-py3-none-any.whl

# Confirm the installed version.
/opt/openrig-matrix/venv/bin/openrig-matrix-bridge --version
# Expected: 0.11.0

# Verify the installed artifact matches the release asset digest.
sha256sum openrig_matrix_bridge-0.11.0-py3-none-any.whl
# Compare with the published release asset checksum.

/opt/openrig-matrix/venv/bin/pip show openrig-matrix-bridge
# Record Version and Location for evidence.
```

## 5 — Run doctor

```bash
MATRIX_ACCESS_TOKEN="$(sed -n 's/^MATRIX_ACCESS_TOKEN=//p' \
  /etc/openrig-matrix-bridge/matrix.env)" \
  /opt/openrig-matrix/venv/bin/openrig-matrix-bridge doctor \
  --config /etc/openrig-matrix-bridge/config.yaml
```

All checks must pass. Stop and investigate any failure before starting the
bridge.

## 6 — Start the bridge

```bash
sudo systemctl start openrig-matrix-bridge.service
sudo systemctl status openrig-matrix-bridge.service

# Confirm exactly one bridge process owns the state directory.
pgrep -af 'openrig-matrix-bridge'
# Expected: exactly one PID.
```

## 7 — Live attribution smoke tests

Verify that live attribution works for each prompt shape below. Each test must
produce an answer delivered to the Matrix room as a thread reply, and (where
applicable) a closed qitem.

> If any prompt shape fails attribution, the bridge degrades to
> `response_timeout_minutes` silence for every request — worse than 0.10.0.
> Do not proceed to production use; investigate and report.

### 7a — Plain single-line prompt

1. Send a short plain-text message in the Matrix room.
2. Observe:
   - The message appears as a user message in the agent transcript.
   - An answer is delivered back to the Matrix room as a thread reply.
   - The corresponding qitem reaches a terminal state.

### 7b — Multi-line prompt

1. Send a message with at least two lines (e.g. a question with a code block).
2. Observe the same three outcomes as 7a.

### 7c — Attachment prompt with answer

1. Send a file attachment (e.g. a small PDF or text file) with a question.
2. Observe:
   - The file is delivered to the agent's inbox.
   - An answer referencing the file is delivered to Matrix.
   - The thread and qitem close normally.

### 7d — Prompt inside an existing Matrix thread

1. In an existing thread (a prior answer thread), send a new follow-up message.
2. Observe:
   - The answer is delivered under the **canonical original thread root**, not
     as a new top-level thread.
   - Exactly one answer is delivered (no duplication).
   - The qitem closes normally.

> The 0.10.0 baseline failure for in-thread prompts is `RoomSendError` /
> `M_UNKNOWN`: "Cannot start threads from an event with a relation". This test
> must pass to prove the 0.11.0 canonical thread delivery fix.

### 7e — Nested reply with a related event

1. In Element, use "Reply" on a message that is itself already a reply or has
   a relation (e.g. reply to a threaded answer).
2. Observe:
   - The bridge resolves the relation chain and delivers the prompt.
   - The answer appears under the canonical original thread root.
   - Exactly one answer; no `RoomSendError`.

### 7f — Element X existing-thread case

1. From Element X, send a message inside an existing thread.
2. Observe:
   - The answer is delivered under the canonical thread root.
   - The Element X client renders the reply within the thread.

> Element X threaded-reply compatibility was manually observed but not
> repository-evidenced. This test produces that evidence.

### Recording evidence

For each test (7a–7f), record:

- The Matrix event ID of the sent message.
- The Matrix event ID of the answer reply.
- The thread root event ID (for 7d–7f).
- The qitem ID and its final state.
- A redacted summary (no private identifiers, homeserver URLs, tokens, or
  credentials).

## 8 — Restart verification

```bash
sudo systemctl restart openrig-matrix-bridge.service
sudo systemctl status openrig-matrix-bridge.service

# Confirm exactly one bridge process.
pgrep -af 'openrig-matrix-bridge'
# Expected: exactly one PID.

# Repeat one plain prompt (7a) and confirm the answer arrives.
```

## 9 — Rollback

If 0.11.0 is not acceptable, restore the pre-upgrade state. **Do not run
0.10.0 against 0.11.0 state** — the 0.11.0 schema adds fields (steering
groups) that 0.10.0 would misinterpret as plain prompts.

```bash
# 1. Stop the 0.11.0 bridge.
sudo systemctl stop openrig-matrix-bridge.service
pgrep -af 'openrig-matrix-bridge'
# Expected: no output.

# 2. Move the current 0.11.0 state aside (do not delete yet).
STATE_DIR="/root/.local/share/openrig-matrix-bridge"
mv "$STATE_DIR" "${STATE_DIR}.0.11.0-aside"

# 3. Restore the verified backup created in step 3.
cp -a "$BACKUP_DIR/state" "$STATE_DIR"
cp -a "$BACKUP_DIR/config.yaml" /etc/openrig-matrix-bridge/config.yaml

# 4. Reinstall 0.10.0.
/opt/openrig-matrix/venv/bin/pip install openrig_matrix_bridge-0.10.0-py3-none-any.whl

/opt/openrig-matrix/venv/bin/openrig-matrix-bridge --version
# Expected: 0.10.0

# 5. Run doctor and start.
MATRIX_ACCESS_TOKEN="$(sed -n 's/^MATRIX_ACCESS_TOKEN=//p' \
  /etc/openrig-matrix-bridge/matrix.env)" \
  /opt/openrig-matrix/venv/bin/openrig-matrix-bridge doctor \
  --config /etc/openrig-matrix-bridge/config.yaml

sudo systemctl start openrig-matrix-bridge.service
sudo systemctl status openrig-matrix-bridge.service

# 6. Confirm one process and a working prompt.
pgrep -af 'openrig-matrix-bridge'
# Expected: exactly one PID.

# 7. Only after verifying 0.10.0 works, remove the aside copy.
rm -rf "${STATE_DIR}.0.11.0-aside"
```

> The rollback restores the crypto store from the backup. Without it, old
> encrypted history cannot be decrypted. Never delete the crypto store.

## Evidence checklist

Before handing off, the operator records:

| Item | Value |
|---|---|
| Candidate SHA | |
| Release asset wheel digest (sha256) | |
| Installed wheel hash matches asset | |
| Installed version (`--version`) | |
| `pip show` output (version, location) | |
| Doctor result | |
| 7a plain prompt: event IDs, qitem ID, state | |
| 7b multi-line prompt: event IDs, qitem ID, state | |
| 7c attachment prompt: event IDs, qitem ID, state | |
| 7d in-thread prompt: event IDs, thread root, qitem | |
| 7e nested reply: event IDs, thread root, qitem | |
| 7f Element X thread: event IDs, thread root, qitem | |
| Restart: post-restart prompt result | |
| Single-instance check (one PID before and after) | |

No tokens, MXC URIs, bearer headers, homeserver URLs, credentials, or
non-synthetic user IDs in recorded evidence.

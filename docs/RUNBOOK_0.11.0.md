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
| State directory | `/var/lib/openrig-matrix-bridge` |
| systemd unit | `openrig-matrix-bridge.service` |
| Launcher | `/usr/local/sbin/openrig-matrix-start` |

The launcher (`openrig-matrix-start`) obtains `MATRIX_ACCESS_TOKEN` from the
secret manager before exec. On this host, `/usr/local/bin/bw-field` provides
the token via Vaultwarden. A generic install must inject the token through
whatever secret manager is in use — the runbook shows the wrapper path; adapt
it to your credential source.

The production venv has no `pip` binary. Use `uv pip install --python
/opt/openrig-matrix/venv/bin/python` for all package operations.

## 0 — Prepare rollback artifacts

Before replacing the current install, build or retain a known 0.10.0 wheel so
that rollback (step 9) does not depend on network access or the editable
install still being present.

```bash
# If a 0.10.0 wheel already exists, save its path and hash:
ROLLBACK_WHEEL="/root/openrig-matrix-rollback-7719795/openrig_matrix_bridge-0.10.0-py3-none-any.whl"
sha256sum "$ROLLBACK_WHEEL"
# Expected (current host): 6e5f353ec0be5ec2a65d754206c89bdf26f7a147a82019da4779da6d2a111a98

# If no wheel exists, build one from the deployed source:
cd /path/to/openrig-matrix-bridge-source
git checkout v0.10.0  # or the deployed commit
python -m build --wheel --outdir /root/openrig-matrix-rollback/
sha256sum /root/openrig-matrix-rollback/openrig_matrix_bridge-0.10.0-py3-none-any.whl
```

Do not proceed until a verified 0.10.0 rollback wheel exists.

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

`--version` prints only the Python package version, not a commit SHA.

```bash
openrig-matrix-bridge --version
# Expected: 0.10.0

uv pip show --python /opt/openrig-matrix/venv/bin/python openrig-matrix-bridge
# Record Version and Location.
```

## 3 — Back up state and config

Back up the state directory and config without exposing secrets. The backup
must be restorable for rollback (step 9) and crypto-store recovery.

```bash
STATE_DIR="/var/lib/openrig-matrix-bridge"
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

Download the exact release wheel. Hash the wheel file itself before installing
so the evidence records the artifact that was actually deployed.

```bash
WHEEL_PATH="/root/openrig_matrix_bridge-0.11.0-py3-none-any.whl"

# Hash the exact wheel file and compare with the published release asset digest.
sha256sum "$WHEEL_PATH"
# Must match the digest published alongside the candidate SHA.

# Install.
uv pip install --python /opt/openrig-matrix/venv/bin/python "$WHEEL_PATH"

# Confirm the installed version.
openrig-matrix-bridge --version
# Expected: 0.11.0

uv pip show --python /opt/openrig-matrix/venv/bin/python openrig-matrix-bridge
# Record Version and Location for evidence.
```

Record the expected source SHA separately — the wheel hash proves artifact
identity; the source SHA proves provenance. These are different things:
`pip show` and `--version` alone do not prove the exact candidate source.

## 5 — Run doctor

Inject the token from the secret manager and run doctor:

```bash
# Generic form — adapt to your secret manager:
MATRIX_ACCESS_TOKEN="$(your-secret-manager-command)" \
  openrig-matrix-bridge doctor --config /etc/openrig-matrix-bridge/config.yaml

# On this host (Vaultwarden via bw-field):
MATRIX_ACCESS_TOKEN="$(/usr/local/bin/bw-field matrix-bridge password)" \
  openrig-matrix-bridge doctor --config /etc/openrig-matrix-bridge/config.yaml
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
STATE_DIR="/var/lib/openrig-matrix-bridge"
mv "$STATE_DIR" "${STATE_DIR}.0.11.0-aside"

# 3. Restore the verified backup created in step 3.
cp -a "$BACKUP_DIR/state" "$STATE_DIR"
cp -a "$BACKUP_DIR/config.yaml" /etc/openrig-matrix-bridge/config.yaml

# 4. Reinstall 0.10.0 from the rollback wheel prepared in step 0.
ROLLBACK_WHEEL="/root/openrig-matrix-rollback-7719795/openrig_matrix_bridge-0.10.0-py3-none-any.whl"
uv pip install --python /opt/openrig-matrix/venv/bin/python "$ROLLBACK_WHEEL"

openrig-matrix-bridge --version
# Expected: 0.10.0

# 5. Run doctor and start.
MATRIX_ACCESS_TOKEN="$(your-secret-manager-command)" \
  openrig-matrix-bridge doctor --config /etc/openrig-matrix-bridge/config.yaml

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
| Wheel file sha256 (hashed before install) | |
| Published release asset digest matches | |
| Expected source SHA | |
| Installed version (`--version`) | |
| `uv pip show` output (version, location) | |
| Doctor result | |
| 7a plain prompt: event IDs, qitem ID, state | |
| 7b multi-line prompt: event IDs, qitem ID, state | |
| 7c attachment prompt: event IDs, qitem ID, state | |
| 7d in-thread prompt: event IDs, thread root, qitem | |
| 7e nested reply: event IDs, thread root, qitem | |
| 7f Element X thread: event IDs, thread root, qitem | |
| Restart: post-restart prompt result | |
| Single-instance check (one PID before and after) | |
| Rollback wheel sha256 (prepared in step 0) | |

No tokens, MXC URIs, bearer headers, homeserver URLs, credentials, or
non-synthetic user IDs in recorded evidence.

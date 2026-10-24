# OpenRig Matrix Bridge

[![test](https://github.com/ssalentin/openrig-matrix-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/ssalentin/openrig-matrix-bridge/actions/workflows/test.yml)

Give every live OpenRig Codex seat a private, encrypted Matrix room so you can
continue the same agent conversation from Element without starting a second
agent.

The bridge resolves the seat through the OpenRig CLI, injects prompts into its
existing terminal, and reads the seat's native Codex transcript for progress
and the final answer. Matrix is the user interface; OpenRig remains the agent
runtime.

This README is the first-time setup path. For the evidence behind individual
product claims, see [Verification and evidence](docs/VERIFICATION.md) and
[Product positioning](docs/POSITIONING.md).

## What works

- One private Matrix room per configured Codex seat, with no second agent.
- Per-room status in the room topic and `dev.openrig.agent_status` state.
- A refreshed Matrix typing indicator while an answer is in progress.
- FIFO prompts per agent, with a single editable pinned queue card.
- Encrypted text, images, voice notes, decisions, and polls in encrypted rooms.
- Canonical thread delivery for top-level prompts and prompts that are already
  thread replies, with bounded commentary/final-delivery failure handling.
- Local NVIDIA Parakeet transcription; audio is not sent to a transcription
  service.
- Silent agent feedback when the owner reacts to a bridge-authored message.
- Durable OpenRig queue items for Matrix prompts, with configurable priority.
- Markdown rendering, including tables, in Matrix-capable clients.

The supported integration is Codex-only. See [Known limits](#known-limits)
before treating a mocked unit test as proof of live Matrix behavior.

## Prerequisites

Before installing, arrange:

- Linux or another environment that can run Python 3.11+.
- OpenRig with one or more running and ready Codex seats.
- A working `rig` command whose `rig ps --nodes -A --json` output includes
  those seats.
- Python 3.11+, `pip`, and a writable state directory.
- `ffmpeg` if voice note transcription is enabled.
- A dedicated Matrix bot account and an access token for one bot device.
- The bot's full Matrix user ID, its device ID, and the owner's full Matrix user
  ID.
- HTTPS access to the Matrix homeserver. The first `setup` also downloads the
  approximately 465 MB Parakeet model unless `--skip-model` is used.

Quick prerequisite checks:

```bash
python3 --version
command -v rig ffmpeg
rig ps --nodes -A --json
```

`setup` discovers every running Codex seat across the available rigs. Stop or
exclude seats you do not want represented.

### Matrix credentials

Create a dedicated bot session with your Matrix client or administrative
tooling and retain its device ID. Put the access token in a process environment
or secret manager; do not put it in YAML or commit it.

For a foreground evaluation, prove that the token and homeserver match before
creating the bridge configuration:

```bash
read -rsp 'Matrix bot access token: ' MATRIX_ACCESS_TOKEN
export MATRIX_ACCESS_TOKEN
printf '\n'

read -r -p 'Matrix homeserver URL: ' MATRIX_HOMESERVER
curl -fsS \
  -H "Authorization: Bearer ${MATRIX_ACCESS_TOKEN}" \
  "${MATRIX_HOMESERVER%/}/_matrix/client/v3/account/whoami"
```

The response should contain the bot's Matrix user ID and device ID. Clear the
token from your shell when the evaluation is finished:

```bash
unset MATRIX_ACCESS_TOKEN MATRIX_HOMESERVER
```

## Install from this checkout

No release is published as part of this documentation change. Until a packaged
release is available, install from the repository:

```bash
git clone https://github.com/ssalentin/openrig-matrix-bridge.git
cd openrig-matrix-bridge
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
openrig-matrix-bridge --version
```

The console entry point and bundled avatars are part of the installed package.
The automated build/install smoke checks are documented in
[Verification and evidence](docs/VERIFICATION.md).

## OpenRig setup

This package does not install OpenRig or Codex. Install and start them through
their own setup, then return here with `rig` on `PATH`.

1. Start the Codex seats that should appear in Matrix.
2. Confirm the caller and live inventory:

   ```bash
   rig whoami --json
   rig ps --nodes -A --json
   ```

3. Confirm each configured seat has `sessionStatus: "running"` and
   `runtime: "codex"`.
4. Keep the Codex home consistent. The setup command records
   `--codex-home` and the bridge uses it when a transcript path must be found
   by the Codex session ID.

`setup` uses the canonical session name from the node inventory. A stopped
seat can remain configured, but it will appear offline and cannot accept
prompts until it is running again. If your OpenRig installation does not expose
the human registry used by the default setup path, use
`--no-human-registration`.

See the [OpenRig project](https://www.openrig.dev/) for installation and seat
management instructions.

## Configure and start

Set the token in the environment. Gather the bot user ID, bot device ID, and
owner user ID from your Matrix account configuration.

```bash
export MATRIX_ACCESS_TOKEN='...'

openrig-matrix-bridge setup \
  --homeserver 'https://matrix.example.org' \
  --user-id '@openrig-bot:matrix.example.org' \
  --device-id 'OPENRIG_BRIDGE' \
  --allowed-user '@owner:matrix.example.org' \
  --config "$HOME/.config/openrig-matrix-bridge/config.yaml"
```

This command:

1. Finds running Codex seats with `rig ps --nodes -A --json`.
2. Writes a private configuration with one mapping per seat.
3. Registers the owner as an OpenRig human recipient with a reusable connector
   binding. The binding is a routing marker; the bridge performs Matrix
   delivery itself.
4. Renames the bot profile and device to `OpenRig`.
5. Downloads and checksum-verifies the Parakeet model unless `--skip-model` is
   present.

Use `--no-human-registration` if you do not want the setup command to modify the
OpenRig human registry. Use `--without-voice` for a text-only bridge. Use
`--skip-model` to finish setup now and download later:

```bash
MATRIX_ACCESS_TOKEN='...' openrig-matrix-bridge download-model \
  --config "$HOME/.config/openrig-matrix-bridge/config.yaml"
```

Review the generated file without printing a token (there should not be one):

```bash
stat -c '%a %n' "$HOME/.config/openrig-matrix-bridge/config.yaml"
grep -nEi 'token|password|secret' "$HOME/.config/openrig-matrix-bridge/config.yaml" || true
```

Run the doctor before starting the long-lived process:

```bash
MATRIX_ACCESS_TOKEN='...' openrig-matrix-bridge doctor \
  --config "$HOME/.config/openrig-matrix-bridge/config.yaml"
```

`doctor` checks the token with Matrix `whoami`, state/media-directory
writability, live Codex session resolution, and model presence. A missing
Parakeet model is a warning unless the bridge cannot later download it.

Start the bridge in the foreground:

```bash
MATRIX_ACCESS_TOKEN='...' openrig-matrix-bridge run \
  --config "$HOME/.config/openrig-matrix-bridge/config.yaml"
```

On the first run the bridge creates one private, encrypted room for each
agent, invites the first configured owner, and sets stable room names. Accept
the invitations in Matrix. A declined invitation, or the owner later leaving a
room, is treated as a rejection: the bridge leaves that room and does not
recreate it automatically. Remove the `rejected_rooms` entry from
`state.json` before restarting if you want to opt the agent back in.

## Verify the product manually

Use the same owner account that appears in `matrix.allowed_users`.
The quoted topic words `bereit` and `arbeitet` are the bridge's current German
status literals; the words mean “ready” and “working.”

1. Check that the room name is stable and its topic shows `bereit` while the
   Codex seat is idle.
2. Send an ordinary message. The bot should show a typing indicator, the topic
   should move to `arbeitet`, and the final answer should appear in the thread.
3. Send a second message while the first is active. It should remain queued and
   the pinned queue card should count it.
4. Attach a PNG, JPEG, GIF, or WebP image. The agent receives a local file path
   under the configured media inbox.
5. Send a short voice note when transcription is enabled. The transcript is
   posted before the agent prompt is forwarded.
6. React to a bridge-authored message. The reaction is forwarded as silent
   feedback and is not mirrored as a visible answer.
7. Publish a decision (see below) and select an option. The choice is returned
   to the responsible agent.

The automated integration harness and its observed limits are documented in
[Verification and evidence](docs/VERIFICATION.md). Unit tests alone do not prove
live Matrix delivery.

## Matrix device verification

The first connection from a new client may show an unverified bot device. The
bridge currently supports device-to-device emoji SAS verification:

1. In Element, open the bot device details and start **Verify by emoji**.
2. The bridge posts seven emojis in the `Advisor` room, or another configured
   room if no `Advisor` room exists.
3. Compare them with Element and click **They match**.
4. Type `ja` in that Matrix room to confirm the bridge side.

Verification applies to the specific Element device. `matrix-nio` does not
currently provide complete cross-signing setup, so verification does not
automatically trust every device on the owner account.

Live Element SAS verification is not covered by the automated test suite. The
verification flow and cross-signing limitation remain tracked in
[Issue #1](https://github.com/ssalentin/openrig-matrix-bridge/issues/1); do not
describe them as fully verified until a real client check is recorded.

## OpenRig behavior

Configured agent keys map one-to-one to live Codex sessions. Duplicate sessions
are rejected by configuration validation.

With `openrig.queue_inbound_messages: true`:

- Each accepted owner message creates a real OpenRig queue item for the target
  seat.
- The item uses the configured `inbound_message_priority` (`urgent` by
  default), the `matrix,inbound` tags, and `--no-nudge` because the bridge
  already delivers the prompt.
- The bridge claims the item when work starts and closes it after the Matrix
  answer is delivered.
- Additional Matrix requests remain FIFO behind the active request.
- A single pinned queue-status message is edited as the count changes and
  unpinned when the queue drains.

The bridge identity is `matrix-bridge@kernel` by default. Change
`openrig.sender_session` only if that identity is intentional in your OpenRig
installation. Set `queue_inbound_messages: false` only for installations that
do not want Matrix work represented in the OpenRig queue.

New OpenRig human updates and decisions are mirrored only when their source
agent's configured session matches. The bridge records a watermark on its
first scan and does not replay the prior queue on first start.

## Decisions and polls

Publish a queue-backed decision into the agent's room:

```bash
openrig-matrix-bridge decision \
  --agent advisor \
  --qitem qitem-20261024000000-example \
  --config "$HOME/.config/openrig-matrix-bridge/config.yaml"
```

The command reads the queue item's summary, body, and optional
`humanQuestions`. A written brief is pinned. Structured questions with two to
four choices are published as Matrix polls; the brief and polls are removed
from the pinned set after all structured questions are answered.

## Images and explicit messages

Send an encrypted image into an agent's room:

```bash
openrig-matrix-bridge send-image \
  --agent advisor \
  --path /tmp/screenshot.png \
  --caption 'Screenshot of the current view'
```

Send an explicit, formatted message into the room:

```bash
openrig-matrix-bridge send-message \
  --agent advisor \
  --body-file /tmp/report.md
```

These commands use the running bridge's encrypted Matrix state. Plain terminal
conversation is not mirrored automatically.

## Security and E2EE

- New rooms are private and created with `m.megolm.v1.aes-sha2` encryption.
- Only one owner is invited when rooms are created. Messages from senders who
  are not listed in `matrix.allowed_users` are ignored.
- The access token is read from `MATRIX_ACCESS_TOKEN`; it is not stored in YAML.
- Encrypted image uploads are encrypted before upload. Remote Markdown images
  are shown as references and are not embedded or fetched.
- Voice audio is processed locally after the model is downloaded. The model
  archive is verified against a pinned SHA-256 checksum.
- State, the Matrix encryption store, and media inbox contain sensitive data.
  Keep the state directory private (`0700` directories and `0600` files) and
  back it up securely.
- Losing the Matrix encryption store can make previously encrypted history
  unreadable even if the room still exists.
- Matrix homeserver administrators can still see room membership and message
  metadata, even when message content is encrypted.

Do not commit tokens, device private keys, recovery material, raw encryption
state, unredacted private MXIDs, room IDs, homeserver addresses, or media.

## systemd

The repository includes a service template in
[`deploy/openrig-matrix-bridge.service`](deploy/openrig-matrix-bridge.service)
and a launcher in [`deploy/openrig-matrix-start`](deploy/openrig-matrix-start).
Adapt `User`, `HOME`, executable paths, and the state directory to the account
that owns the bot session.

Install the files:

```bash
sudo install -Dm755 deploy/openrig-matrix-start /usr/local/sbin/openrig-matrix-start
sudo install -Dm644 deploy/openrig-matrix-bridge.service \
  /etc/systemd/system/openrig-matrix-bridge.service
sudo systemctl daemon-reload
```

Inject `MATRIX_ACCESS_TOKEN` through your secret manager. For a simple
evaluation, use a root-owned environment file:

```bash
sudo install -d -m700 /etc/openrig-matrix-bridge
sudoedit /etc/openrig-matrix-bridge/matrix.env
# Add: MATRIX_ACCESS_TOKEN=...

sudo systemctl edit openrig-matrix-bridge.service
```

Add this unit override:

```ini
[Service]
EnvironmentFile=/etc/openrig-matrix-bridge/matrix.env
```

Then place the generated configuration at the path expected by the launcher
(`/etc/openrig-matrix-bridge/config.yaml` by default), check it, and start:

```bash
MATRIX_ACCESS_TOKEN="$(sed -n 's/^MATRIX_ACCESS_TOKEN=//p' /etc/openrig-matrix-bridge/matrix.env)" \
  openrig-matrix-bridge doctor --config /etc/openrig-matrix-bridge/config.yaml
sudo systemctl enable --now openrig-matrix-bridge.service
sudo systemctl status openrig-matrix-bridge.service
journalctl -u openrig-matrix-bridge.service -f
```

Use a proper secret manager rather than a plaintext environment file on a
multi-user production host.

## Test setup

From the repository:

```bash
python -m pip install -e '.[dev]'
python -m pip install ruff
python -m pytest -q
ruff check .
```

The current automated suite covers configuration validation, OpenRig
resolution and queue commands, transcript parsing, room/status construction,
queue notices, Markdown rendering, decisions, encrypted image content,
transcription model state, avatars, and failure handling at the unit boundary.
With `uv` installed, the equivalent lint command is `uv tool run ruff check .`.

The reproducible Matrix integration harness requires Docker:

```bash
.venv/bin/python tools/matrix_integration.py \
  --timeout 60 \
  --source-commit "$(git rev-parse HEAD)"
```

It starts disposable Synapse on loopback, runs the bridge as a separate
process, and writes a redacted JSON report to
`artifacts/integration/report.json`. The OpenRig side is a deterministic
subprocess fixture, so this proves Matrix protocol integration but not a live
Codex model turn. See [Verification and evidence](docs/VERIFICATION.md) for the
observed checks, exact boundaries, and gaps.

## Troubleshooting

### `no running Codex seats found`

Start OpenRig and its Codex seats, then confirm that:

```bash
rig ps --nodes -A --json
```

shows a running entry whose runtime is `codex`. Pass the correct
`--rig-command` and `--codex-home` to `setup`.

### `MATRIX_ACCESS_TOKEN is not set`

Export the token in the process that runs `doctor`, `run`, or an outbox command.
You can also set `OPENRIG_MATRIX_CONFIG` to a non-default configuration path.

### `Matrix whoami failed`

Check the homeserver URL, token, bot user ID, and device ID in the generated
configuration. A token from another homeserver cannot be used.

### `duplicate agent session`

Two keys refer to the same Codex seat. Keep one mapping per live session.

### Room was rejected

If the owner declines an invitation or leaves, the bridge persists a
`rejected_rooms` tombstone and leaves that room. Remove the specific entry from
`state.json`, then restart the bridge if recreation is intended.

### Voice transcription is unavailable

Confirm `ffmpeg` is installed and run:

```bash
openrig-matrix-bridge download-model \
  --config "$HOME/.config/openrig-matrix-bridge/config.yaml"
```

The first download is large and requires network access. Unit tests verify the
model checksum and readiness, not recognition quality.

### Old encrypted history cannot be decrypted

Restore the state directory, especially the Matrix crypto store, from a secure
backup. Recreating the state does not recreate the old encryption session.

### Old room keeps appearing unverified

Complete the device SAS flow, then re-open the device details in Element. Old
events can retain their historical warning. See [Matrix device
verification](#matrix-device-verification) and Issue #1.

## Known limits

- Only Codex seats are supported. Other OpenRig runtimes are out of scope.
- Live Element SAS and the broader cross-signing strategy are not fully verified
  by the repository; see Issue #1.
- A mocked Matrix client is not a substitute for the committed integration run.
- Voice transcription quality and every audio format are not covered by a
  deterministic end-to-end test; model readiness and checksum configuration
  are unit-tested.
- OpenRig status is polled, not a push subscription. Very brief state changes
  can be missed between refreshes.
- Ordinary terminal chat is not mirrored to Matrix.
- The bridge supports one invited owner by default. Additional accounts must
  be intentionally added to the room and allowlist.
- Image support is limited to PNG, JPEG, GIF, and WebP and the configured byte
  limit.
- The default user-facing status and help strings are German.

## Upgrade and help

1. Stop the bridge and back up the generated config and state directory.
2. Upgrade the installed package.
3. Run `openrig-matrix-bridge doctor`.
4. Start the bridge and repeat the manual checks above.

Do not delete the Matrix crypto store during an upgrade.

- [Repository](https://github.com/ssalentin/openrig-matrix-bridge)
- [Issues](https://github.com/ssalentin/openrig-matrix-bridge/issues)
- [Evidence matrix](docs/VERIFICATION.md)
- [Positioning and demo copy](docs/POSITIONING.md)
- [Artifact manifest](docs/artifacts/README.md)
- [Environment support issue #1](https://github.com/ssalentin/openrig-matrix-bridge/issues/1)
- [Release-readiness issue #2](https://github.com/ssalentin/openrig-matrix-bridge/issues/2)

For configuration field details, use
[`config.example.yaml`](config.example.yaml) as a starting point and
`openrig-matrix-bridge --help` for the installed command surface.

## License

MIT

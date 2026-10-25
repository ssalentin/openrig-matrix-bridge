# Installation and operations

This page covers prerequisites, credentials, installation, OpenRig discovery,
configuration, service deployment, troubleshooting, and upgrades.

## What you need

- A Linux or equivalent host with Python 3.11+.
- OpenRig installed, with one or more running Codex seats.
- The `rig` command available to the bridge, plus the Codex home used by those
  seats.
- A dedicated Matrix bot account, its homeserver URL, its user ID, its device
  ID, and a token for that one bot device.
- The full Matrix user ID of the person who should own the rooms.
- HTTPS access to the Matrix homeserver.
- `ffmpeg` and about 465 MB of model storage if local voice transcription is
  enabled.

The bot access token belongs in an environment variable or secret manager.
Never put it in this repository, a public issue, or a chat message.

If you already have OpenRig and a Matrix account, you do not need to edit the
configuration by hand. Install the 0.12.0 wheel, expose the bot token as
`MATRIX_ACCESS_TOKEN`, and run `setup`. Setup discovers the live Codex seats,
writes the private configuration, installs the Relay profile, and registers
the owner binding.

## Copy-ready OpenRig setup brief

Copy the block below into OpenRig after replacing every angle-bracket
placeholder. It tells OpenRig to inspect the existing installation, configure
the bridge, run health checks, and verify the real Matrix rooms without asking
for the token in chat.

```text
Set up the OpenRig Matrix bridge in <ABSOLUTE_REPOSITORY_PATH>.

Goal: give every currently running Codex seat one private encrypted Matrix room
without starting a second agent.

Before changing anything:
- run `rig whoami --json`;
- run `rig ps --nodes -A --json`;
- list the running Codex seats you intend to expose;
- confirm the bridge's `rig` command and Codex home;
- stop and ask me if any of those values are ambiguous.

Use these non-secret Matrix values:
- homeserver: <MATRIX_HOMESERVER_URL>
- bot user ID: <BOT_MATRIX_USER_ID>
- bot device ID: <BOT_MATRIX_DEVICE_ID>
- owner Matrix user ID: <OWNER_MATRIX_USER_ID>

The bot access token is already available to your process through
MATRIX_ACCESS_TOKEN in the environment or secret manager. Do not ask me to paste
the token into chat, write it into a repository file or YAML, print it, or add it
to an issue. If the variable is absent, stop and tell me how to expose it to the
process safely.

Then:
1. Create or reuse a virtual environment and install the 0.12.0 wheel from the
   GitHub Release, or install the package from the checkout when evaluating
   the repository.
2. Verify the Matrix token with the homeserver `whoami` endpoint without
   printing it.
3. Run `openrig-matrix-bridge setup` with the homeserver, bot user ID, bot
   device ID, owner user ID, live `rig` command, Codex home, explicit config
   path, and state directory. Use `--no-human-registration` only if the
   installed OpenRig human registry is unavailable. Use `--skip-model` if you
   want to defer the large voice model download.
4. Inspect the generated YAML without printing credentials. It must not contain
   an access token or private encryption state.
5. Run `openrig-matrix-bridge doctor --config <CONFIG_PATH>` and stop on a hard
   failure.
6. Start the bridge in the foreground with
   `openrig-matrix-bridge run --config <CONFIG_PATH>`, or install the adapted
   systemd unit from this page, and wait for one private room per configured
   agent.
7. Verify that the owner was invited, each encrypted room exists once, the room
   topic becomes ready, and the configured agent mapping is stable.
8. Report the config path, exact commands, health result, seat-to-room mapping,
   invite status, and any remaining Matrix device-verification steps.

Do not claim success until room creation and owner invitations are observed.
Do not describe device SAS verification or cross-signing as complete until a
real trusted client confirms it.
```

## Detailed prerequisites

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

## Matrix credentials

Create a dedicated bot session with your Matrix client or administrative
tooling and retain its device ID. Put the access token in a process environment
or secret manager; do not put it in YAML or commit it.

To find the bot user ID and device ID from a normal Matrix client:

1. Sign in to the dedicated bot account.
2. Open the account/profile screen and copy the full user ID, such as
   `@openrig-bot:matrix.example.org`.
3. Open the session or device list and copy the bot device ID.
4. Sign back in as your personal owner account and copy its full user ID.

For a foreground evaluation, prove that the token, homeserver and bot identity
match before creating the bridge configuration:

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

## Install 0.12.0 or this checkout

Download the wheel and source distribution attached to the GitHub 0.12.0
release, verify their published SHA-256 digests, and install the wheel in a
virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install /path/to/openrig_matrix_bridge-0.12.0-py3-none-any.whl
openrig-matrix-bridge --version
```

For repository development or an installation before the release assets are
attached, install from a checkout:

```bash
git clone https://github.com/ssalentin/openrig-matrix-bridge.git
cd openrig-matrix-bridge
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
openrig-matrix-bridge --version
```

The console entry point, selected Relay identity, and bundled agent avatars are
part of the installed package. Automated build and clean-install checks are
documented in [Verification and evidence](VERIFICATION.md). For an upgrade on
an existing host, follow the [0.12.0 runbook](RUNBOOK_0.12.0.md).

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
4. Keep the Codex home consistent. The setup command records `--codex-home`
   and the bridge uses it when a transcript path must be found by the Codex
   session ID.

`setup` uses the canonical session name from the node inventory. A stopped seat
can remain configured, but it will appear offline and cannot accept prompts
until it is running again. If your OpenRig installation does not expose the
human registry used by the default setup path, use
`--no-human-registration`.

See the [OpenRig project](https://www.openrig.dev/) for installation and seat
management instructions.

## Configure and start

Set the token in the environment. Gather the bot user ID, bot device ID, and
owner user ID from your Matrix account configuration.

The homeserver must use HTTPS. Plain `http://` is accepted only for loopback
test servers such as `http://127.0.0.1:8008`; the bridge rejects credentials,
queries and fragments in the homeserver URL. Configuration agent keys must
start with an alphanumeric character and contain only letters, digits,
underscores or hyphens (maximum 64 characters), so they remain safe per-agent
directory names.

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
4. Renames the bot profile and device to `OpenRig`, and installs the Relay
   profile image.
5. Downloads and checksum-verifies the Parakeet model unless `--skip-model` is
   present.

The command above is the complete non-interactive setup path when OpenRig and
Matrix are already working. If an argument is omitted, `setup` prompts for the
missing value and prints where to find the corresponding Matrix ID.

### What setup infers and what you provide

| Value | Source |
|---|---|
| Homeserver URL, owner Matrix user ID | You provide them (flag or prompt); setup cannot infer them. |
| Bot user ID and device ID | You provide them, but when `MATRIX_ACCESS_TOKEN` is set setup takes both from the homeserver `whoami` answer for that token and prints a note if they differ from yours. |
| Codex seats and their sessions | Discovered from `rig ps --nodes -A --json`. |
| Access token | Only from the environment. Setup checks it with `whoami`, never accepts it as an argument, and never prints or writes it. |

Setup stops with a next step in these states:

| State | What setup says to do |
|---|---|
| Config file already exists | Pass `--replace-config`, or choose another `--config` path. |
| A bridge is already running for the state directory | Stop that bridge first; only one bridge may own a state directory and device. |
| Token invalid, expired or forbidden | Create a new bot device token and export it again. |
| Homeserver unreachable | Check the homeserver URL and network. |
| No running Codex seat, or a duplicate seat name | Start a Codex seat and confirm it with `rig ps --nodes -A --json`. |
| `rig` not found or not answering | Pass `--rig-command` with the full path. |
| OpenRig human registry unavailable | Pass `--no-human-registration`. |
| Voice model download fails | The config is already written; pass `--skip-model` to defer the download. |

Use `--no-human-registration` if you do not want the setup command to modify the
OpenRig human registry. Use `--without-voice` for a text-only bridge. Use
`--skip-model` to finish setup now and download later:

```bash
MATRIX_ACCESS_TOKEN='...' openrig-matrix-bridge download-model \
  --config "$HOME/.config/openrig-matrix-bridge/config.yaml"
```

Review the generated file without printing a token; there should not be one:

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

On the first run the bridge creates one private, encrypted room for each agent,
invites the first configured owner, and sets stable room names. Accept the
invitations in Matrix. A declined invitation, or the owner later leaving a
room, is treated as a rejection: the bridge leaves that room and does not
recreate it automatically. Remove the `rejected_rooms` entry from `state.json`
before restarting if you want to opt the agent back in.

For production upgrades, use [the 0.12.0 runbook](RUNBOOK_0.12.0.md) to verify
the exact artifact, stop the old process, back up state, run live
plain/multi-line/attachment/thread smoke tests, and roll back safely.

## systemd

The repository includes a service template in
[`deploy/openrig-matrix-bridge.service`](../deploy/openrig-matrix-bridge.service)
and a launcher in
[`deploy/openrig-matrix-start`](../deploy/openrig-matrix-start). Adapt `User`,
`HOME`, executable paths, and the state directory to the account that owns the
bot session.

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
events can retain their historical warning. See
[Matrix device verification](SECURITY.md#matrix-device-verification) and
Issue #1.

## Upgrade and help

1. Stop the bridge and back up the generated config and state directory.
2. Upgrade the installed package.
3. Run `openrig-matrix-bridge doctor`.
4. Start the bridge and repeat the manual checks in the
   [user guide](USER_GUIDE.md#verify-the-product-manually).

Do not delete the Matrix crypto store during an upgrade.

For configuration field details, use
[`config.example.yaml`](../config.example.yaml) as a starting point and
`openrig-matrix-bridge --help` for the installed command surface.

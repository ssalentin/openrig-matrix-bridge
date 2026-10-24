# OpenRig Matrix Bridge

**A private Matrix room for every OpenRig agent, with live status and local
voice transcription.**

The bridge uses the OpenRig CLI to inject prompts into the live Codex terminal
and watches that seat's native JSONL transcript for the final answer. It does
not start duplicate agents, does not need a separate chat gateway, and does not
require an OpenRig-native Matrix connector.

## Live status in Matrix rooms

The bridge keeps each room topic synchronized with the agent's actual state.
Room names stay stable, so Matrix clients do not turn the status symbol into
the generated room logo:

| Room topic | Meaning |
|---|---|
| `🟢 bereit · advisor-lead@kernel` | Online and idle |
| `🟡 arbeitet · advisor-lead@kernel` | Working on a request |
| `🟡 🧠 denkt · advisor-lead@kernel` | Reasoning before or between actions |
| `🟡 🔎 sucht · advisor-lead@kernel` | Searching files, code, or queue data |
| `🟡 🌐 Netzwerk · advisor-lead@kernel` | Explicit network or web tool activity |
| `🟡 📝 bearbeitet Dateien · advisor-lead@kernel` | Editing files or applying patches |
| `🟡 ✍️ schreibt · advisor-lead@kernel` | Composing an answer |
| `🟡 ⏸️ wartet auf Freigabe · advisor-lead@kernel` | Waiting for required input |
| `🟠 Prompt fehlgeschlagen · advisor-lead@kernel` | Actionable bridge/gateway error |
| `⚫ offline · advisor-lead@kernel` | No live agent process |

While an answer is being generated, Matrix also shows the bot typing. The same
state is published as the `dev.openrig.agent_status` room event for clients and
tooling that want structured data. The status event includes the high-level
state, activity detail, and activity icon.

Because one Matrix bot account can only have one global presence, status is
represented per room. OpenRig can use one bot for any number of agents.

## Features

- One private Matrix room per configured agent seat.
- End-to-end encrypted Matrix rooms by default.
- Local speech-to-text for encrypted Matrix voice messages with NVIDIA Parakeet.
- Encrypted PNG, JPEG, GIF, and WebP transfer in both directions.
- Native Matrix rendering for Markdown, including headings, lists, code, and tables.
- Explicit proactive messages from an agent into its Matrix room.
- Per-agent room status: `🟢 bereit`, `🟡 arbeitet`, `🟠 Fehler`, `⚫ offline`.
- A refreshed Matrix typing indicator while the agent is working.
- Pinned decision briefs and Element-compatible multiple-choice polls.
- Automatic mirroring of new OpenRig human updates and decisions into the source agent's room.
- Device verification with Matrix emoji SAS.
- One allow-listed Matrix user; messages from everybody else are ignored.
- Replies are posted in a Matrix thread under the original prompt.
- Per-agent FIFO: a second prompt waits until the current answer is complete.
- No secrets in the repository or configuration file.
- Systemd-ready, with persisted Matrix sync and E2EE state.

## Quick start

Prerequisites:

- A Matrix account for the bridge bot and an access token.
- OpenRig with Codex seats running in tmux or Herdr.
- `rig`, Python 3.11+, and `ffmpeg`.

Create the configuration from the currently running Codex seats:

```bash
MATRIX_ACCESS_TOKEN='...' openrig-matrix-bridge setup \
  --homeserver https://matrix.example.org \
  --user-id '@openrig-bot:matrix.example.org' \
  --device-id OPENRIG_BRIDGE \
  --allowed-user '@owner:matrix.example.org'
```

`setup` discovers the live seats, creates one agent mapping per seat, and
downloads the Parakeet model. It also registers the Matrix owner as OpenRig's
human recipient. Current OpenRig releases require a Slack-typed connector
binding for that registry entry. The bridge uses it as a routing marker while
performing Matrix delivery itself, so this connector does not need a Slack
token. Use
`--no-human-registration` to skip this step, or `--replace-human` to refresh an
existing registration. Diagnose the installation at any time:

```bash
MATRIX_ACCESS_TOKEN='...' openrig-matrix-bridge doctor
```

Start the bridge:

```bash
MATRIX_ACCESS_TOKEN='...' openrig-matrix-bridge run
```

On first start, it creates a private encrypted room for each configured agent
and invites the allow-listed owner.

## Verify the bot

The first Matrix device may show an "unverified device" warning. Open the bot's
device details in Element, start **Verify by emoji**, compare the seven emojis,
then type `ja` in the `Advisor` room. Confirm the matching SAS in
Element. This verifies the specific Element device; repeat it for other devices
that should trust the bot.

## Decisions in Matrix

Matrix calls these **pinned messages** rather than sticky messages. Element
shows them in the room's Pinned messages panel. The bridge can also publish a
structured OpenRig decision as encrypted Matrix polls with two to four choices:

```bash
openrig-matrix-bridge decision \
  --agent advisor \
  --qitem qitem-20261024000000-example
```

The command reads the queue item's summary, body, and optional
`humanQuestions`, then queues an encrypted decision for the running bridge.
The bridge pins the brief and poll events. A selected answer is handed back to
the agent as a normal queued prompt. Once all structured questions are
answered, their pinned events are removed automatically. Queue items without
structured choices are still pinned as a complete written decision brief.

## Images and formatting

Images sent by the owner are downloaded, decrypted, type-checked, and stored in
`media.inbox_dir/<agent>/`. The responsible agent receives the local file path
as a queued prompt. The bridge accepts PNG, JPEG, GIF, and WebP up to
`media.max_image_bytes`.

An agent can send an image back to its Matrix room through the running bridge:

```bash
openrig-matrix-bridge send-image \
  --agent advisor \
  --path /tmp/screenshot.png \
  --caption 'Screenshot der aktuellen Ansicht'
```

The file and its Matrix event are encrypted separately before upload. The
command reports the Matrix event ID once delivery completes.

Agent answers are converted from Markdown to Matrix's `org.matrix.custom.html`
format in addition to their plain-text fallback. Tables, headings, lists,
emphasis, code blocks, and ordinary links therefore render natively in Element.
Raw HTML is escaped, and remote Markdown images are shown as references rather
than loaded automatically.

An agent can also send an explicitly requested, formatted Matrix message:

```bash
openrig-matrix-bridge send-message \
  --agent advisor \
  --body-file /tmp/report.md
```

This command sends through the running bridge so the message is encrypted with
the correct Matrix room session. Ordinary terminal conversation is not mirrored
automatically.

## Human handoffs from OpenRig

Set `openrig.human_destinations` to the human queue addresses that should be
visible in Matrix. New queue items whose source is a configured agent are then
mirrored into that agent's room:

- `humanIntent: decision` becomes a pinned decision message and, when
  `humanQuestions` is present, encrypted Matrix polls.
- `humanIntent: update` becomes a normal formatted Matrix message.

The first bridge start records the existing queue as a watermark and does not
replay old history. Items created while the bridge is running are delivered
once. This makes Matrix a first-class delivery surface rather than an
afterthought to another chat gateway.

## Current scope

This first version targets Codex seats. It resolves their live transcript and
Codex thread through `rig ps --nodes --json --full`, then sends prompts with
`rig send --raw`. Other runtimes can be added by extending the transport and
transcript adapters.

Direct messages from the owner to configured agents are supported. Structured
OpenRig decisions can be published explicitly with the `decision` command;
ordinary replies are not guessed to be approvals.

Voice messages are transcribed locally with NVIDIA Parakeet TDT 0.6B v3 through
`sherpa-onnx`. The model supports 25 European languages, including German, and
detects the language automatically. Audio is never uploaded to an external
transcription service. The runtime is a normal Python dependency; the 465 MB
INT8 model weights are downloaded once into `transcription.download_root` and
verified against a pinned SHA-256 checksum.

## Configuration

`setup` normally writes the configuration. To configure manually, copy
`config.example.yaml` to a private location such as
`/etc/openrig-matrix-bridge/config.yaml`. Put the Matrix access token in an
environment variable or credential helper and expose it to the process as
`MATRIX_ACCESS_TOKEN`.

The Matrix bot needs permission to create private rooms and invite the
allow-listed user. On first start, the bridge creates and persists one room per
agent in `state_dir`.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
uvx ruff check .
```

For a systemd deployment, use a credential helper or secret manager to inject
`MATRIX_ACCESS_TOKEN`. Do not put the token in `config.yaml` or a checked-in
environment file.

## How a message flows

1. The allowed Matrix user writes in an agent-specific room.
2. The bridge resolves the live Codex seat and native transcript through OpenRig.
3. It records the current transcript offset.
4. It submits the text directly with `rig send --raw`.
5. It tails the native Codex JSONL transcript.
6. The next completed final assistant answer is posted to the Matrix thread.
7. Queued messages for the same agent are submitted in order.

## Security

- Keep the configuration file free of tokens.
- Restrict `allowed_users` to the owner's full MXID.
- Keep the Matrix and OpenRig state accessible only to trusted local users.
- Keep the media inbox private and apply a suitable retention policy.
- Use a dedicated bot account where possible.

## License

MIT


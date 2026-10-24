# OpenRig Matrix Bridge

**A private Matrix room for every OpenRig agent, with live status and local
voice transcription.**

The bridge uses the local [Moshi gateway](https://getmoshi.app/docs/debug-gateway)
to inject prompts into the live Codex terminal and watches the same agent
transcript for the final answer. It does not start duplicate agents and does
not require an OpenRig-native Matrix connector.

## Live status in Matrix rooms

The bridge keeps each room topic synchronized with the agent's actual state.
Room names stay stable, so Matrix clients do not turn the status symbol into
the generated room logo:

| Room topic | Meaning |
|---|---|
| `🟢 bereit · advisor-lead@kernel` | Online and idle |
| `🟡 arbeitet · advisor-lead@kernel` | Working on a request |
| `🟠 Prompt fehlgeschlagen · advisor-lead@kernel` | Actionable bridge/gateway error |
| `⚫ offline · advisor-lead@kernel` | No live agent process |

While an answer is being generated, Matrix also shows the bot typing. The same
state is published as the `dev.openrig.agent_status` room event for clients and
tooling that want structured data.

Because one Matrix bot account can only have one global presence, status is
represented per room. OpenRig can use one bot for any number of agents.

## Features

- One private Matrix room per configured agent seat.
- End-to-end encrypted Matrix rooms by default.
- Local speech-to-text for encrypted Matrix voice messages with NVIDIA Parakeet.
- Encrypted PNG, JPEG, GIF, and WebP transfer in both directions.
- Native Matrix rendering for Markdown, including headings, lists, code, and tables.
- Per-agent room status: `🟢 bereit`, `🟡 arbeitet`, `🟠 Fehler`, `⚫ offline`.
- A refreshed Matrix typing indicator while the agent is working.
- Pinned decision briefs and Element-compatible multiple-choice polls.
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
- `moshi-hook` running on the same host.
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
downloads the Parakeet model. Diagnose the installation at any time:

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

## Current scope

This first version targets Codex seats because it resolves their session and
transcript through `moshi-hook` state under
`~/.local/state/moshi/codex-sessions/`. Other runtimes can be added by extending
the gateway and transcript adapters.

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
2. The bridge resolves that seat's current Codex session from Moshi hook state.
3. It submits the text through `POST /v1/prompt` on the loopback Moshi gateway.
4. It tails the mapped JSONL transcript.
5. The next completed final assistant answer is posted to the Matrix thread.
6. Queued messages for the same agent are submitted in order.

## Security

- Keep the configuration file free of tokens.
- Restrict `allowed_users` to the owner's full MXID.
- Keep both Matrix and Moshi state directories mode `0700`.
- Keep the media inbox private and apply a suitable retention policy.
- Use a dedicated bot account where possible.

## License

MIT


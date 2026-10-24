# OpenRig Matrix Bridge

Give selected OpenRig agents their own private Matrix rooms through one bot.

The bridge uses the local [Moshi gateway](https://getmoshi.app/docs/debug-gateway)
to inject prompts into the live Codex terminal and watches the same agent
transcript for the final answer. It does not start duplicate agents and does
not require an OpenRig-native Matrix connector.

## Features

- One private Matrix room per configured agent seat.
- End-to-end encrypted Matrix rooms by default.
- One allow-listed Matrix user; messages from everybody else are ignored.
- Replies are posted in a Matrix thread under the original prompt.
- Per-agent FIFO: a second prompt waits until the current answer is complete.
- No secrets in the repository or configuration file.
- Systemd-ready, with persisted Matrix sync and E2EE state.

## Current scope

This first version targets Codex seats because it resolves their session and
transcript through `moshi-hook` state under
`~/.local/state/moshi/codex-sessions/`. Other runtimes can be added by extending
the gateway and transcript adapters.

Direct messages from the owner to configured agents are supported. Proactive
OpenRig human queue items and interactive approval buttons are a later adapter;
they are not silently conflated with an agent chat.

## Configuration

Copy `config.example.yaml` to a private location such as
`/etc/openrig-matrix-bridge/config.yaml`. Put the Matrix access token in an
environment variable or credential helper and expose it to the process as
`MATRIX_ACCESS_TOKEN`.

The Matrix bot needs permission to create private rooms and invite the
allow-listed user. On first start, the bridge creates and persists one room per
agent in `state_dir`.

## Run

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
MATRIX_ACCESS_TOKEN='...' \
  .venv/bin/openrig-matrix-bridge --config /etc/openrig-matrix-bridge/config.yaml
```

For a systemd deployment, use a credential helper or secret manager to inject
`MATRIX_ACCESS_TOKEN`. Do not put the token in `config.yaml` or a checked-in
environment file.

Unit tests:

```bash
.venv/bin/pytest
```

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
- Use a dedicated bot account where possible.

## License

MIT


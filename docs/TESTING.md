# Testing and release validation

This repository separates fast automated checks from executable integration
evidence. The distinction matters: a unit test can prove transcript or Matrix
payload handling, but it cannot prove that a homeserver accepted an encrypted
room and delivered a message.

## Fast local checks

Create the development environment and run the checks used by CI:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

Build both distributable artifacts, then inspect them and install each into
separate clean virtual environments:

```bash
.venv/bin/python -m build
.venv/bin/python tools/verify_release.py
```

`verify_release.py` checks the exact wheel/sdist filenames, wheel license and
avatar data, source-distribution support files, console entry point, installed
distribution metadata, and the 32-avatar runtime pool. It runs from temporary
working directories so the source checkout cannot satisfy imports accidentally.

## Executable Matrix integration

The integration harness requires Docker. It starts a disposable, digest-pinned
Synapse container, creates synthetic bridge and owner accounts, generates a
bridge configuration through the public `setup` command, and runs the bridge
against a real Matrix homeserver:

```bash
.venv/bin/python tools/matrix_integration.py
```

The successful report is written to
`artifacts/integration/report.json`. The ephemeral container, homeserver
database, Matrix stores, bridge state, logs and fake transport state are deleted
after the run. Use `--keep-workdir` only for local diagnosis; never commit that
state. Use `--report PATH` to select another output location and `--timeout N`
to bound individual wait operations.

The harness uses `tools/fake_openrig.py` as a separate deterministic OpenRig CLI
process. It exercises the bridge's real `rig ps`, `rig send`, and `rig queue`
subprocess contracts, transcript polling, and final-answer extraction. It does
not prove delivery into a live Codex terminal. That limitation is recorded in
the report.

### Automated integration coverage

| Check | What is exercised |
|---|---|
| `setup-and-doctor` | Public `setup` and `doctor` commands against Synapse and the fake OpenRig transport |
| `room-status-avatar` | Private room creation, owner invite/join, Megolm room state, managed avatar, ready topic, structured status |
| `encrypted-chat-queue-typing` | Owner text, typing indicator, visible commentary, final answer, priority qitem lifecycle, raw `m.room.encrypted` event |
| `fifo-queue-notice` | Per-agent FIFO ordering and the pinned queue notice |
| `silent-reaction-feedback` | Reaction forwarding without a visible reply |
| `encrypted-images` | Owner-to-agent encrypted PNG storage and agent-to-owner `send-image` byte round trip |
| `voice-disabled-path` | Encrypted audio handling with voice deliberately disabled |
| `decision-and-poll` | Pinned decision brief, MSC3381 poll, encrypted response, prompt forwarding, unpin |
| `proactive-message` | Explicit encrypted `send-message` |
| `restart-recovery` | Room/state reuse and status recovery across bridge restart |
| `doctor-invalid-token` | Representative authentication failure |
| `rejected-room-recovery` | Owner leave tombstone and no room recreation after restart |

### Manual or unproven paths

- A real Codex seat receiving the Matrix prompt and completing its native
  transcript is not automated. The harness uses a fake OpenRig subprocess.
- Parakeet model download and real speech transcription are not run. The
  harness deliberately uses `setup --without-voice` and no network model.
- Element device-to-device emoji SAS and cross-signing require a human client
  action and remain tracked by issue #1.
- Federation, production homeserver policy, and trust between real user-owned
  devices are outside this local harness.

## Report schema

The integration report uses `schema_version: 1`. Its top-level keys are:

- `status`: `pass` or `fail`
- `duration_seconds`: wall-clock runtime
- `target`: package name and version
- `environment`: disposable homeserver, Docker image digest, transport, voice
  and SAS limits
- `results`: checks with `id`, `status`, `automated` and human-readable
  `evidence`
- `known_gaps`: claims not exercised by this run
- `bridge_state_summary`: final room, processed-event and rejection counts
- `redaction`: what was deliberately omitted from evidence
- `error`: present only for failed runs

The report contains synthetic localhost fixture identities but no access
tokens, room IDs, private infrastructure, Matrix recovery data or unredacted
terminal state. Bridge and homeserver logs stay in the temporary work directory
and are removed on success.

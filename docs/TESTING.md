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
not prove delivery into a live Codex terminal. The fake transport refuses a
second concurrent `rig send` while its simulated agent is busy. That limitation
is recorded in the report.

### Automated integration coverage

| Check | What is exercised |
|---|---|
| `setup-and-doctor` | Public `setup` and `doctor` commands against Synapse and the fake OpenRig transport |
| `room-status-avatar` | Private room creation, owner invite/join, Megolm room state, managed avatar, ready topic, structured status |
| `encrypted-chat-queue-typing` | Owner text, typing indicator, visible commentary, thread-related final answer, recorded pending -> in-progress -> done qitem history, raw `m.room.encrypted` event |
| `fifo-queue-notice` | Per-agent FIFO ordering with second-send timestamp after first completion, plus the pinned queue notice |
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

- `status`: `pass`, `fail`, or `invalid` when source provenance is dirty or
  mismatched
- `evidence_valid`: whether the result is bound to a clean source commit
- `duration_seconds`: wall-clock runtime
- `target`: package name/version plus `source.commit`, provenance source and
  `worktree_clean`
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

## Evidence provenance

The harness reads the checked-out Git commit and tracked worktree status. A
report is valid only when HEAD is a full 40-character commit, matches an optional
`--source-commit SHA`, and has no tracked modifications. Untracked output such
as the generated report and review notes is excluded from the dirty check so it
does not invalidate the source under test.

A commit cannot contain the hash that will result from committing its own test
evidence. The final flow therefore is:

1. commit the harness change;
2. run the harness from that clean commit;
3. commit the regenerated evidence as a separate evidence-only commit;
4. have QA and review bind their verdict to that final evidence commit and cite
   the source commit recorded in the report.

Use an explicit expected commit when running from automation that already knows
the tested SHA:

```bash
.venv/bin/python tools/matrix_integration.py \
  --timeout 60 \
  --source-commit "$(git rev-parse HEAD)"
```

A dirty tracked worktree or mismatched `--source-commit` produces
`status: invalid` and `evidence_valid: false`; it is not valid release proof.

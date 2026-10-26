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
python3 tools/check_markdown_links.py
npm ci
npx --no-install markdownlint-cli2
```

Build both distributable artifacts, then inspect them and install each into
separate clean virtual environments:

```bash
.venv/bin/python -m build
.venv/bin/python tools/verify_release.py
```

`verify_release.py` checks the exact wheel/sdist filenames, wheel license and
avatar data, source-distribution support files, console entry point, installed
distribution metadata, and the 32-avatar runtime pool. The source-distribution
checks require the release plan, positioning, testing, verification, and
artifact-manifest documents. It runs from temporary working directories so the
source checkout cannot satisfy imports accidentally.

CI runs the unit, lint, documentation, artifact, and Matrix integration jobs on
every push and pull request. The Markdown linter is installed from the pinned
`package-lock.json`, so the job does not resolve a moving latest release.

Fast tests also drive the fake OpenRig transport’s deterministic create, claim,
finish, and fail error injection. These prove bridge command-error handling and
restart state, but they are not counted as Matrix integration checks.

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

The issue #3 attachment run is preserved separately at
`docs/artifacts/issue-3-integration-report.json`; it binds the report to source
commit `fe6f1271758773dc90ac73975f03763515db7732`.

The harness uses `tools/fake_openrig.py` as a separate deterministic OpenRig CLI
process. It exercises the bridge's real `rig ps`, `rig send`, and `rig queue`
subprocess contracts, transcript polling, turn attribution, and final-answer
extraction. It does not prove delivery into a live Codex terminal. For steering
checks only, the fake transport opts into pane-style input during a busy turn.
It can record the input immediately or retain it without a transcript user
record and materialize it as a later synthetic turn after the original
completion. Other checks still verify that a busy turn rejects a normal new
prompt. That limitation is recorded in the report.

### Matrix timing-failure policy

The voice-transcript exactly-once assertion is intentionally strict and must
not be weakened or suppressed. A single failure records the exact source,
preserves its failed report, and permits one rerun at the same commit because
the failure has appeared as a transient timing result. The handoff must cite
both runs; a passing rerun does not erase the first failure from the evidence.
Two consecutive failures at the same commit are a release blocker.

The voice check waits for the transcript prompt to reach the fake OpenRig
transport before treating an empty bridge pending list as idle. This separates
the deliberate status-before-enqueue ordering from the exact-one prompt
assertion and prevents the evidence reader from observing the gap as a missing
forward.

### Automated integration coverage

The single-message checks send unrelated attachments seconds apart, so they run
with timing correlation off (`openrig.burst_inactivity_seconds: 0`). The
`related-*` checks restart the bridge on the production defaults (45 s
inactivity, 300 s span, receipt off) and start each scenario more than 45 s
after the previous owner event, which makes a full run several minutes longer.
`--only CHECK ...` runs selected checks for development; such a report is
`partial` with `evidence_valid: false` and is never release evidence.

| Check | What is exercised |
|---|---|
| `ephemeral-matrix` | The harness started a disposable Synapse on loopback; the container and state directory are removed at exit unless `--keep-workdir` is set |
| `setup-and-doctor` | Public `setup` and `doctor` commands against Synapse and the fake OpenRig transport, plus a second `setup` without `--replace-config` that must fail with that next step |
| `room-status-avatar` | Private room creation, owner invite/join, Megolm room state, managed avatar, ready topic, structured status |
| `encrypted-chat-queue-typing` | Owner text, typing indicator, activity-labelled commentary card, separate thread-related final answer, recorded pending -> in-progress -> done qitem history, raw `m.room.encrypted` event |
| `threaded-prompt-restart` | Encrypted owner prompt already in `m.thread`; commentary and final answer under the canonical root; single qitem closure; bridge typing and `arbeitet` status for the reply; bridge restart without duplicate prompt or response and with typing restored for the in-flight request |
| `qitem-claim-crash-recovery` | Test-only process exit immediately after the bridge claimed an OpenRig qitem; the restart recognizes the in-progress qitem, resumes the unsent prompt exactly once and completes the same qitem |
| `answer-crash-recovery` | Test-only process exit immediately after final answer send; stable transaction retry produces no duplicate Matrix event and reconciles one qitem |
| `activity-card-crash-recovery` | Issue #39: process exit right after Matrix accepted the initial activity card and before its event ID was recorded; the restart retries with the stable transaction ID and the request keeps exactly one card that every edit targets, one answer, one completed qitem |
| `commentary-redaction` | Issue #70: an advisor-style commentary with two internal `/root/.openrig/...` paths reaches the room's activity card as the full progress text with each path replaced by `[lokaler Pfad]`; no bridge message carries the path bytes |
| `threaded-files` | Threaded PDF and markerless audio status/final messages use the canonical root and each creates one completed qitem |
| `fifo-queue-notice` | Per-agent FIFO ordering with second-send timestamp after first completion; no pinned queue card while the first request runs alone (#86), then one pinned queue card that distinguishes the active prompt from the ordered, sanitized waiting previews; between the two queued turns the room never turns `bereit` and bridge typing has no gap; the card is unpinned once the queue drains |
| `steering-modes-and-batch` | Disposable-Synapse queue/steer/auto semantics, ordered same-thread batching with all source IDs, canonical-root answer metadata, top-level/cross-thread isolation, qitem closure, and rejection of an unrelated human transcript turn |
| `steering-cross-thread` | Steer mode (#87): a new top-level message and a reply in another thread sent during a slow active turn become one ordered steering batch that marks their origin; neither starts its own turn, exactly one answer is posted in the active thread with all three source IDs, and all three qitems close once |
| `steering-fallback-restart` | Refused busy injection preserves the full batch for one normal fallback turn; a bridge crash immediately after pane injection recovers admission from the transcript without resending |
| `steering-late-admission` | A busy-pane batch remains absent from the transcript past the initial grace and materializes after the root completion as the next turn; exactly one transcript input, one final answer, one Matrix answer, both source IDs, and both qitems are observed without fallback resend; after the follow-up answer the room never turned `bereit` and bridge typing continued until the queue drained |
| `acceptance-reaction` | With the optional receipt enabled, exactly one 👍 related to each accepted owner event, including every member of a steering batch; a bridge crash right after the reaction send recovers the pending acknowledgement on restart and reuses it instead of sending another; no reaction, no acceptance record and no transport delivery for an untrusted sender |
| `acceptance-reaction-default-off` | With the receipt off (default), an accepted owner message is queued, typing is observed, it is answered and its qitem is done, and it gets no bridge reaction and no acceptance record |
| `silent-reaction-feedback` | Reaction forwarding without a visible reply |
| `encrypted-images` | Owner-to-agent encrypted PNG storage and agent-to-owner `send-image` byte round trip |
| `voice-status-edit` | Encrypted voice note, deterministic local transcript, one threaded progress event edited by `m.replace`, no transcript-only reply, one forwarded prompt and qitem closure |
| `encrypted-files` | Encrypted PDF and unknown-binary transfer in both directions; markerless audio fallback; generated safe inbox paths; private admitted snapshots; `m.file` metadata and byte-preserving decryption |
| `agent-file-delivery` | A nonzero shipping-label-shaped PDF sent with `send-file --json`: the returned event ID is the exact encrypted `m.file` the owner receives, it decrypts to the reported SHA-256, and empty, missing and oversize files fail before any outbox request or Matrix event |
| `attachment-restart-recovery` | A generic-file request queued while stopped is delivered exactly once after restart from its snapshot after the source is changed, and records the observed Matrix event ID |
| `element-x-gallery` | One encrypted `dm.filament.gallery` with four HEIC-labelled items and JPEG previews reaches the agent as one prompt and one completed qitem, items in source order with filename, MIME type, size and dimensions, originals and previews decrypted byte-for-byte |
| `gallery-restart` | A crash after the first of four gallery items was stored resumes the other three without downloading the first again; one prompt, one qitem |
| `related-gallery-then-text` | Production defaults: a top-level gallery and a top-level text about 1 s and about 30 s later reach the agent linked, in source order, digest-verified, one qitem per event, every answer under the gallery; after 30 s the text is a continuation restating every item |
| `related-voice-text-attachments` | Voice note with injected transcription latency, then text, image and PDF one second apart: nothing overtakes the transcript; order, digests, qitems and threading as above |
| `related-galleries-and-files` | Two galleries, a PDF, a CSV and a text in one exchange arrive linked to the first gallery in source order with every path digest-verified |
| `related-restart` | Restart between parts, then a crash after a PDF's intake was persisted and before its download: the download resumes once (one stored copy) and the PDF and a later text arrive linked to the gallery |
| `related-unrelated-isolation` | Two plain top-level texts two seconds apart stay separate requests with their own answers; a thread reply joins only its thread |
| `related-thread` | A gallery and a text sent into a thread more than 45 s after its root join the root's context by relation; a top-level message sent in between stays separate |
| `legacy-pending-reconcile` | Bridge 0.10.0 pending requests (no phase fields) for done and canceled qitems are reconciled against OpenRig at startup: no claim, prompt or qitem update, no message for done, exactly one notice for a deliverable canceled one, an undeliverable one dropped without a retry loop, and a second restart changes nothing |
| `voice-disabled-path` | A voice-message-marked encrypted audio event reaches transcription handling while voice is deliberately disabled |
| `decision-and-poll` | Pinned decision brief, MSC3381 poll, encrypted response, prompt forwarding, unpin |
| `proactive-message` | Explicit encrypted `send-message` |
| `restart-recovery` | Room/state reuse and status recovery across bridge restart |
| `doctor-invalid-token` | `doctor` exits non-zero with the invalid-or-expired-token next step |
| `rejected-room-recovery` | Owner leave tombstone and no room recreation after restart |

### Manual or unproven paths

- A real Codex seat receiving the Matrix prompt and completing its native
  transcript is not automated. The harness uses a fake OpenRig subprocess.
- Parakeet model download and recognition quality are not run. The harness
  enables the voice path with `--skip-model` and a separate-process deterministic
  transcriber wrapper, then exercises the real Matrix status/edit lifecycle.
- Element device-to-device emoji SAS and cross-signing require a human client
  action and remain tracked by issue #1.
- Federation, production homeserver policy, and trust between real user-owned
  devices are outside this local harness.
- Attachment malware scanning, client-specific previews, and operator-managed
  inbox retention are not implemented.
- The integration OpenRig transport is deterministic. It exercises the bridge's
  crash boundaries, but a live Codex process crash remains unverified.

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

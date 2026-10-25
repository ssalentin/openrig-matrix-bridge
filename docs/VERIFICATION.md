# Verification and evidence

This note separates what the repository proves from what merely looks plausible
in its implementation. It is the source for product positioning and launch
copy. Update it with the observed command, commit, environment, and artifact
path whenever a manual or integration check is performed.

## Evidence levels

| Level | Meaning |
|---|---|
| Automated unit | The implementation path runs against a mocked Matrix/OpenRig boundary. Useful for contracts and failure handling, not proof of a live server. |
| Automated integration | The committed harness starts or connects to a real Matrix homeserver and exercises the public bridge path. The exact harness boundary and retained artifact must be named. |
| Manual | A person or documented harness performed the check in a named environment. The result and limitations must be recorded in an artifact. |
| Unverified | The repository does not currently prove the claim. It must not appear as a launch promise. |

## Matrix client compatibility

These rows keep three different claims separate: automated Matrix protocol
behavior, owner-observed client behavior, and real device trust.

| Scenario | Evidence level | Result | Remaining limit |
|---|---|---|---|
| Encrypted rooms, messages and thread relations | Automated integration | Disposable Synapse and a real `matrix-nio` client completed the committed protocol checks. | Does not prove Element rendering or JavaScript-client behavior. |
| Element X threaded replies | Manual, owner-observed | The owner manually exercised Element X threaded replies successfully on 2026-10-24. | No screenshot is available, Element X version/device details are not recorded, and this is not screenshot-backed evidence. |
| Prompts already inside a thread | Automated integration and unit | The canonical input root is preserved, including across bridge restart; threaded text, image, file and voice replies keep the original root. Relation normalization also covers rich replies that do not declare `m.thread`. | A complete owner-screenshot matrix for every Element X nested-reply UI state is still absent. |
| Element device SAS and removed unverified-device warnings | Manual, unverified | Pending issue #1. Persisted restart/timeout state is unit-tested, not live approved. | A synthetic protocol client or mock must not be presented as owner-device trust. |
| Element Web SAS | Manual, unverified | A real Element Web session against disposable Synapse displayed `(User verification unavailable)` for the bridge account on 2026-10-24. | Cross-signing/device trust must be implemented and verified against a real client before claiming success. |
| Element Web thread and queue rendering | Manual, screenshot-backed | The synthetic owner-to-agent flow and pinned queue card rendered in Element Web 1.12.32 against disposable Synapse. | The screenshots illustrate rendering only; the OpenRig transport was deterministic, device trust remained unverified, and historical-device warnings were excluded from the committed crops. |

The owner observation means only that the exercised Element X reply flow
rendered and behaved correctly in that session. It does not establish SAS,
cross-signing, warning removal, federation, or compatibility for every Element X
version.

## Provenance

The final 0.11.0 source under test is
`3b21b72330a9b0810457a3ee4d11fdd2c144ae90`. It is a descendant of `main`
and includes the accepted runbook. The final Matrix report records this source
commit and `target.source.worktree_clean: true`.

The commit that publishes these evidence edits follows the tested source
commit. A commit cannot contain the hash it will derive after changing its own
contents, so the QA/review handoff names that evidence commit explicitly and
binds its verdict to the source SHA recorded here and in the report.

## Product claim matrix

| Product claim | Implementation | Automated evidence | Live/reproducible evidence | Current status |
|---|---|---|---|---|
| One private room per configured agent and no duplicate agent process | [room creation](../src/openrig_matrix_bridge/bridge.py) is `_create_rooms()`; [configuration](../src/openrig_matrix_bridge/config.py) rejects duplicate sessions; prompts use `rig send --raw` against the resolved live seat rather than a second runtime. | [`test_rejects_duplicate_sessions`](../tests/test_config.py#L57), [`test_sends_prompt_through_rig_cli_with_bridge_identity`](../tests/test_openrig.py#L100), [`test_rejected_agent_room_is_not_recreated`](../tests/test_bridge.py#L280). | Disposable Synapse accepted one invited room; restart preserved the mapping without creating a duplicate. The harness covered one configured agent, so multi-agent cardinality remains unit/config evidence. | **Verified for one agent end to end; multi-agent cardinality unit-tested.** |
| Live status and typing | Status is published to `m.room.topic` and `dev.openrig.agent_status`; typing is refreshed by `_typing_loop()` and cleared when a request finishes. | [`test_status_uses_topic_and_structured_event_instead_of_room_name`](../tests/test_bridge.py#L122). | The owner client observed the ready topic, structured status, and bridge typing indicator on disposable Synapse. | **Verified on live Matrix protocol path; no Element screenshot required for this claim.** |
| Live activity card | The editable commentary card maps only OpenRig's safe activity taxonomy to `Arbeitet`, `Bereit`, or `Wartet auf Eingabe`; unknown runtime state uses the generic working label. The latest visible commentary is redacted for tokens, URI userinfo with or without a username, authorization and cookie/session headers, local paths, fenced raw output, and length before display. Terminal delivery persists a retry intent and does not advance the request or queue until the card edit or explicit fallback is acknowledged. Status refresh uses the same per-agent lock as request completion, so a delayed edit cannot overwrite the terminal card. | [`test_activity_card.py`](../tests/test_activity_card.py), [`test_openrig.py`](../tests/test_openrig.py). | The exact-source Synapse harness observes an activity-labelled commentary card while the final answer remains a separate threaded message. Restart retention, concurrent refresh/completion ordering, and completion, timeout, and delivery-failure recovery are covered at the bridge boundary. | **Matrix card and redaction path verified; fine-grained tool activity is not available and is not inferred.** |
| Verification restart and notification recovery | Verification records persist phase/room/disposition. Interrupted confirmation words remain guarded until a new verification supersedes them; `agent: ja` is the explicit ordinary-prompt escape. Interruption/timeout terminal transitions queue durable notifications with stable Matrix transaction IDs, and startup/poll reconciliation retries undelivered notices. | [`test_verification.py`](../tests/test_verification.py) covers indefinite interrupted guarding, explicit bypass, terminal grace/pruning, failed-before-delivery recovery, stable retry IDs, and timeout reconciliation. | QA’s independent probe now shows late interrupted forwarding false and recovered interruption delivery 1; exact-source Matrix integration passes 14/14. | **Automated state-machine recovery verified; real-device SAS and warning removal remain unverified.** |
| Verification outcome edge cases (issue #1) | `_on_verification` fails a SAS that matrix-nio canceled on its MAC (key mismatch, unexpected message) with the Matrix code and tells the owner; an early Element MAC keeps waiting; `ja` after Element confirmed first records `verified` instead of waiting for a MAC that already arrived; a non-emoji start is declined with an explanation; a remote cancel is recorded. `doctor` reads matrix-nio's trust file for owner devices and the latest recorded outcome. | [`test_verification.py`](../tests/test_verification.py) (key mismatch, early MAC, both confirmation orders, remote cancel, non-emoji decline incl. a failing cancel); [`test_cli.py`](../tests/test_cli.py) (doctor report). | None: no real device was used. | **Automated state machine verified; real-device SAS, warning removal and cross-signing remain unverified (issue #1, [cross-signing evaluation](CROSS_SIGNING.md)).** |
| End-to-end encrypted text and room media | `AsyncClientConfig(encryption_enabled=True)` stores E2EE state; newly created rooms include Megolm encryption; image uploads request encryption and verify encryption metadata. | [`test_encrypted_image_is_saved_and_forwarded_to_agent`](../tests/test_bridge.py#L1111), [`test_builds_encrypted_image_message`](../tests/test_media.py#L15). | Synapse reported `m.megolm.v1.aes-sha2`; owner text and image traffic was encrypted through matrix-nio and decrypted back. | **Verified on disposable Synapse; public-server and device-trust policy remain gaps.** |
| Canonical threaded prompt delivery | Inbound `m.thread` roots are preserved; top-level prompts start one request thread; rich replies without a thread use `m.in_reply_to` instead of manufacturing a thread. Commentary delivery fails boundedly (three attempts, then it is skipped) rather than blocking later requests. A final answer that cannot be sent after three attempts fails its qitem and posts a thread notice; the request stays at the head of the queue, and later requests wait, until Matrix accepts that notice. That hold is not bounded yet (Issue #62). | Relation, storage, delivery, and bounded-failure tests in [`test_bridge.py`](../tests/test_bridge.py). | An encrypted owner prompt already inside `m.thread` survived a bridge restart while the fake seat was busy; commentary and final answer each appeared once under the original root and the qitem closed. | **Verified on disposable Synapse with fake OpenRig transport.** |
| Local speech-to-text and voice status | `Transcriber` invokes local `ffmpeg` and `sherpa-onnx`; model readiness and checksum are implemented. Each accepted voice note creates one threaded status event, atomically tracks matrix transaction IDs and terminal intent, and edits that event to the transcript or terminal failure. | Voice lifecycle and fault-boundary tests in [`test_bridge.py`](../tests/test_bridge.py), checksum/model tests in [`test_transcription.py`](../tests/test_transcription.py). | A deterministic separate-process transcriber produced a transcript on disposable Synapse: one threaded progress event was edited by `m.replace`, no transcript-only reply appeared, the prompt was forwarded once, and its qitem closed. | **Matrix edit lifecycle verified; real Parakeet recognition and Element rendering remain gaps.** |
| Restart-idempotent prompt, answer, attachment, and qitem effects | Requests persist prompt/answer/qitem phases before external effects. Prompt ambiguity after restart fails closed; answers use stable Matrix transaction IDs; qitem claim/finish/fail reconcile through queue state; media outbox requests use stable IDs. | [`test_restart_idempotency.py`](../tests/test_restart_idempotency.py) covers simulated process death after prompt/answer sends, qitem claim/finish/fail recovery, answer dedup, and pending completion. Existing media outbox tests cover state-before-rename and recorded-delivery suppression. | Synapse harness kills the bridge immediately after the final answer send; restart produces the same single Matrix event and one completed qitem. | **Verified on disposable Synapse with fake OpenRig transport; live Codex crash behavior remains untested.** |
| Encrypted image transfer in both directions | Incoming images are downloaded, decrypted, type-checked, size-limited, stored, and forwarded as a path. Outgoing `send-image` encrypts uploads and emits an encrypted image event. | [`test_encrypted_image_is_saved_and_forwarded_to_agent`](../tests/test_bridge.py#L1111), [`test_detects_supported_image_signatures`](../tests/test_media.py#L7), [`test_builds_encrypted_image_message`](../tests/test_media.py#L15). | Owner PNG was stored byte-for-byte and forwarded; `send-image` produced an image the owner downloaded and decrypted byte-for-byte. | **Verified on disposable Synapse.** |
| Encrypted arbitrary-file transfer in both directions | Incoming `m.file` events and markerless audio are size-checked, decrypted, stored under generated paths, and forwarded with original metadata. Outgoing `send-file` snapshots regular-file bytes at admission, encrypts the snapshot, emits `m.file`, and persists restart-safe delivery state. | [`test_encrypted_pdf_is_safely_stored_and_forwarded_to_agent`](../tests/test_bridge.py), [`test_markerless_audio_is_routed_to_generic_file_handler`](../tests/test_bridge.py), [`test_publishes_encrypted_generic_file_with_original_metadata`](../tests/test_bridge.py), [`test_send_file_rejects_symlink_swapped_after_precheck`](../tests/test_cli.py), [`test_rejects_tampered_file_snapshot_before_upload`](../tests/test_bridge.py). | PDF, unknown binary, and markerless audio passed through disposable Synapse; restart recovery delivered the admitted snapshot exactly once after the source changed. | **Verified on disposable Synapse.** |
| Pinned decisions and Element-compatible polls | Decisions are written to a durable inbox, published as a pinned brief plus MSC3381 poll events, and selected answers are returned as a queued prompt. | [`test_poll_answer_is_forwarded_and_pinned_decision_is_cleared`](../tests/test_bridge.py#L327), [`test_builds_element_compatible_poll_content`](../tests/test_decisions.py#L36), [`test_reads_unstable_poll_response`](../tests/test_decisions.py#L55). | Brief and poll were pinned; encrypted response returned to the fake transport; completed events were unpinned. | **Protocol path verified; Element rendering not verified.** |
| Reactions as silent feedback | Owner reactions to bridge-authored events are resolved, bounded, forwarded to the agent with `suppress_reply=True`, and excluded from visible answer delivery. | [`test_reaction_to_bot_post_becomes_silent_feedback`](../tests/test_bridge.py#L468), [`test_silent_feedback_does_not_post_agent_answer`](../tests/test_bridge.py#L523). | Owner reaction reached the fake transport as feedback and created no visible bot reply on Synapse. | **Verified at fake-transport/Matrix boundary.** |
| Durable OpenRig queue behavior | Each tracked Matrix input creates a priority qitem with `--no-nudge`, claims it when work starts, closes it after answer delivery, and leaves untracked reactions out of queue creation. | [`test_creates_matrix_qitem_with_priority_and_no_duplicate_nudge`](../tests/test_openrig.py#L159), [`test_claims_and_finishes_qitem_as_destination_agent`](../tests/test_openrig.py#L205), [`test_matrix_message_creates_priority_qitem`](../tests/test_bridge.py#L953). | A Matrix prompt created an urgent fake qitem, moved pending to in-progress to done, and was delivered once. | **Command contract verified; live OpenRig daemon recovery unverified.** |
| FIFO prompt queue and pinned queue card | Per-agent `asyncio.Lock` and pending lists serialize prompts. One queue notice shows the active request plus ordered, source-labelled previews of waiting requests, is edited in place as entries change, reconstructs from pending state after restart, and is unpinned when empty. Unstarted claim retries and busy-seat requests are labelled as the next request; silent feedback is omitted. Real steering fallback containers contribute to the active source count without appearing as future turns. Preview text is sanitized and truncated before display. | Queue-card tests in [`test_bridge.py`](../tests/test_bridge.py) and [`test_steering.py`](../tests/test_steering.py) cover zero/one/multiple entries, in-place edits, drain, restart, redaction, silent-feedback exclusion, unstarted requests and the production steering batch shape. | While the first fake turn was held, Matrix showed the pinned active/waiting preview card and the transport received both prompts in FIFO order. | **Verified on disposable Synapse with fake transport.** |
| Best-effort hybrid queue/steering | `queue`, `steer`, and default `auto` modes keep one active turn; only short follow-ups in the exact active Matrix thread are candidates. An ordered fallback request and one qitem per source event persist before pane injection. Transcript session/turn evidence is required before admission, and unrelated human turns are ignored. | [`test_steering.py`](../tests/test_steering.py) covers mode semantics, same-thread batching, top-level/cross-thread isolation, qitem closure, steering error, completion fallback, restart adoption/ambiguity, and unrelated-turn attribution. | The disposable-Synapse checks exercise queue/steer/auto, canonical-root answer source metadata, busy rejection fallback, and crash-after-injection recovery. | **Integration evidence is recorded for the narrowed best-effort contract only; OpenRig exposes no acknowledged safe-boundary or admission-turn API.** |
| Related messages, galleries and voice arrive as one context | [`bursts.py`](../src/openrig_matrix_bridge/bursts.py) classifies parts by relation and timing and the bridge joins them (`_enqueue_prompt`, `_complete_part` in [`bridge.py`](../src/openrig_matrix_bridge/bridge.py)); settings `openrig.burst_inactivity_seconds` (45) and `openrig.burst_max_span_seconds` (300). | [`test_bursts.py`](../tests/test_bursts.py) and [`test_conversation_context.py`](../tests/test_conversation_context.py). | Harness checks `element-x-gallery`, `gallery-restart`, `related-gallery-then-text` (gallery then text about 1 s and 30 s later on the production defaults), `related-voice-text-attachments`, `related-galleries-and-files`, `related-restart`, `related-unrelated-isolation` and `related-thread`; QA's locked run at the gated head passed 34 of 34 checks (`evidence_valid`). | **Verified on disposable Synapse with fake OpenRig transport.** Element X on a real device is not proven. |
| Agent file delivery is explicit and verifiable | [`run_send_file`](../src/openrig_matrix_bridge/cli.py) snapshots a regular file and, with `--json`, prints `event_id`, `room_id`, filename, MIME type, size and `sha256`; see the [guide](AGENT_FILE_DELIVERY.md). | [`test_cli.py`](../tests/test_cli.py): empty, missing, symlink, unreadable and oversize files fail before anything is queued, the JSON result, and the unchanged plain output. | Harness check `agent-file-delivery`: a generated nonzero PDF is received by the exact returned event ID, decrypts to the reported SHA-256, and empty, missing and oversize files fail before any outbox request. | **Verified on disposable Synapse.** The AgentSpec guidance block is documentation and was not run in a real agent. |
| A qitem the agent already closed counts as finished | [`_finish_inbound_qitem`](../src/openrig_matrix_bridge/bridge.py) reads the qitem before every finish attempt and again after a rejected update (`_inspect_qitem_state`). | [`test_qitem_finish.py`](../tests/test_qitem_finish.py): with the fake OpenRig enforcing OpenRig's note-append rule, the two regressions fail on the pre-fix source with `note_append_fields_not_admitted`; compatibility tests run against the installed `rig` update syntax. | QA's locked diagnostic run: 27 of 27 checks; all 36 fake qitems ended `done` with no duplicate transition. | **Verified on disposable Synapse with fake OpenRig transport.** The production error on a real queue under live load is not proven. |
| Pending requests saved by bridge 0.10 are reconciled at startup | [`_reconcile_legacy_pending`](../src/openrig_matrix_bridge/bridge.py) reads each request's qitem before any claim, prompt, notice or answer: `done` drops the request with no replay, another closed state drops it with at most one best-effort notice, and an open one keeps the fail-closed behavior. | [`test_legacy_pending.py`](../tests/test_legacy_pending.py) uses the 0.10.0 request shape; 14 of its 18 tests fail on the pre-fix source with `qitem_not_claimable`. | Harness check `legacy-pending-reconcile`: four 0.10-shaped requests drain with no claim, prompt or qitem update, one notice for the deliverable canceled one, the undeliverable one dropped without a retry loop, and a second restart changes nothing; QA's locked runs passed 28 of 28 checks at the gated head. | **Verified on disposable Synapse with fake OpenRig transport.** Not proven against a real 0.10 state file; the fixture is reconstructed from the 0.10.0 request shape. |
| A pending 👍 never outlives the setting that enabled it | [`_recover_acceptance_reactions`](../src/openrig_matrix_bridge/bridge.py) marks pending receipts `expired` while `openrig.acceptance_reaction_enabled` is off, so turning it back on never sends an old 👍; the immediate steering scan matches when the active request has no recorded turn id. | [`test_acceptance_reaction.py`](../tests/test_acceptance_reaction.py) and [`test_steering.py`](../tests/test_steering.py): two regressions fail on the pre-fix source (`'sending'` instead of `'expired'`, `'injected_unconfirmed'` instead of `'admitted'`). | No harness check seeds a pending receipt across a restart. | **Verified by unit tests only.** The turn-id fix changes little in a full poll, which rescans in the same cycle. |
| `setup` validates the token and states the next step | [`run_setup`](../src/openrig_matrix_bridge/cli.py) checks `MATRIX_ACCESS_TOKEN` with `whoami` before writing anything, infers the bot user ID and device ID, and stops with a next step in each listed failure state; a lock on the state directory keeps a second bridge off the same state. | [`test_setup.py`](../tests/test_setup.py) and [`test_instance.py`](../tests/test_instance.py). | Harness checks `setup-and-doctor` (a second `setup` without `--replace-config` fails with that next step) and `doctor-invalid-token`; QA's locked runs passed 3 of 3 at the gated head. | **Verified on disposable Synapse with fake OpenRig transport.** Not exercised against a real Element or Element X client. |
| Markdown and safe rich-text rendering | Markdown becomes Matrix HTML; raw HTML is escaped; remote images are rendered as references instead of fetched. | [`test_renders_gfm_table_as_matrix_html`](../tests/test_formatting.py#L4), [`test_escapes_raw_html`](../tests/test_formatting.py#L14), [`test_does_not_embed_remote_markdown_images`](../tests/test_formatting.py#L21). | Client rendering is not automated. Matrix event content can be captured. | **Unit-verified.** |
| Selected Relay identity | Relay is the only shipped symbol. The README embeds local light/dark 256px PNGs as a compact accessible mark; the bot profile uses the contained 256px cross-theme avatar; 32px exports and editable SVGs are packaged under the repository MIT license. | [`test_brand_assets.py`](../tests/test_brand_assets.py), [`test_avatar_pool.py`](../tests/test_avatar_pool.py), [`test_assigns_unique_repo_avatars_and_profile`](../tests/test_bridge.py#L49). | Synapse accepts the Relay profile upload in the Matrix integration harness. Client-specific favicon rendering is not separately automated. | **Identity assets, dimensions, README links, package contents, and Matrix profile upload verified.** |
| Deterministic, collision-free agent avatars | A bundled 32-image pool is assigned once per agent and persisted in state. | [`test_ships_a_large_unique_avatar_pool`](../tests/test_avatar_pool.py#L10), [`test_preserves_valid_assignments_and_never_duplicates`](../tests/test_avatar_pool.py#L20), [`test_new_agents_receive_unused_avatars_deterministically`](../tests/test_avatar_pool.py#L35). | Synapse accepted a managed room avatar on the test room. | **Unit-verified and observed on Synapse.** |

## Current automated checks

Run from the repository:

```bash
python -m pytest -q
python -m pip install ruff
ruff check .
```

Observed results from the clean final source:

| Check | Exact command | Commit | Observed result |
|---|---|---|---|
| Unit tests | `.venv/bin/python -m pytest -q` | `3b21b72330a9b0810457a3ee4d11fdd2c144ae90` | 243 passed in 1.25s |
| Python lint | `.venv/bin/ruff check .` | `3b21b72330a9b0810457a3ee4d11fdd2c144ae90` | All checks passed |
| Markdown structure | `npm ci && npx --no-install markdownlint-cli2` | `3b21b72330a9b0810457a3ee4d11fdd2c144ae90` | 10 files, 0 issues |
| Relative links | `python tools/check_markdown_links.py` | `3b21b72330a9b0810457a3ee4d11fdd2c144ae90` | checked=10, missing=0 |
| Release artifacts | `.venv/bin/python -m build` then `.venv/bin/python tools/verify_release.py` | `3b21b72330a9b0810457a3ee4d11fdd2c144ae90` | Wheel and sdist clean installs passed; 32 avatars present |
| Matrix integration | Exact-source command below | `3b21b72330a9b0810457a3ee4d11fdd2c144ae90` | `evidence_valid: true`, 20/20 checks passed in 49.9s |

With `uv` installed, `uv tool run ruff check .` is equivalent to the lint
command above.

### Issue #3 attachment evidence

The issue #3 source commit `fe6f1271758773dc90ac73975f03763515db7732`
passed:

| Check | Exact command | Observed result |
|---|---|---|
| Unit tests | `.venv/bin/python -m pytest -q` | 135 passed |
| Python lint | `.venv/bin/ruff check .` | All checks passed |
| Matrix integration | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit fe6f1271758773dc90ac73975f03763515db7732` | `status: pass`, `evidence_valid: true`, 17/17 checks in 43.3s |

The machine-readable result is
[`issue-3-integration-report.json`](artifacts/issue-3-integration-report.json).
It covers encrypted PDF and unknown-binary transfer in both directions,
markerless-audio generic routing, generated safe inbox paths, `m.file` metadata,
private admission snapshots, byte-preserving decryption, Megolm encryption on
the wire, and a pending outbox attachment delivered exactly once from its
snapshot after restart and source mutation. The OpenRig transport remains the
deterministic fixture rather than a live Codex model. The rebased run also
preserves the merged #19 canonical-thread delivery behavior, including the
threaded-prompt restart check and threaded generic-file/markerless-audio root
preservation.

### Issue #11 restart-idempotency evidence

The issue #11 source commit
`5b70d345b6598f2ca50faf86216f6ba353166ead` passed:

| Check | Exact command | Observed result |
|---|---|---|
| Full tests | `.venv/bin/python -m pytest -q` | 154 passed |
| Focused restart tests | `.venv/bin/python -m pytest -q tests/test_restart_idempotency.py` | 18 passed |
| Python lint | `.venv/bin/ruff check .` | All checks passed |
| Matrix integration | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit 5b70d345b6598f2ca50faf86216f6ba353166ead` | `evidence_valid: true`, 19/19 checks in 46.6s |

The machine-readable result is
[`issue-11-integration-report.json`](artifacts/issue-11-integration-report.json).
It includes deterministic bridge exits immediately after qitem claim and final
answer send. Restart resumes the claimed-but-unsent prompt exactly once and
reuses the answer transaction ID, returning the same Matrix event and closing
one qitem. Unit fault tests also cover busy-agent recovery, ambiguous prompt
sends, and qitem claim/finish/fail reconciliation.

The covered pre-0.11 migration shape is: requests with `started_at` but no
`prompt_phase` are treated as ambiguous and fail closed rather than being
resent. For those tracked requests, an authoritative done qitem completes
locally without answer replay, while a transient qitem-close error emits one
stable failure and stops polling.

Migration of unstarted legacy pending requests whose qitems are already
terminal is not covered by 0.11.0 and is deferred to issue #41 in milestone
0.12.0.

### Issue #12 qitem lifecycle evidence

Issue #12 source `2b50efd164591880bf3c2443c08fdb0fb0dff60e` passed 167 tests
and the exact-source Matrix harness (`evidence_valid: true`, 19/19 checks in
50.3s). The fake transport injects create, claim, finish, and fail errors.
Bridge tests verify creation failure sends no prompt, claim failure remains
retryable, finish/fail failure remains pending, prompt/timeout failures retain
the request when fail-close fails, and terminal qitems advance FIFO.
The report is
[`issue-12-integration-report.json`](artifacts/issue-12-integration-report.json).

### Matrix restart-observation stability

Source `b1e8d17a1d758da3655deb6eb4aba237d2b0ebbc` uses five-second long polls,
bounded backoff, timeout diagnostics, and a restart barrier requiring durable
`prompt_phase=sent` plus claimed/finishing qitem state. Three consecutive
exact-source runs passed 20/20 checks; the persisted run completed in 54.1s with
`evidence_valid: true`. Report:
[`matrix-stability-report.json`](artifacts/matrix-stability-report.json).

### 0.12.0 steering, acceptance receipt, and fail-closed evidence

The 0.12.0 candidate narrows the steering contract and adds an optional, off-by-default 👍
acceptance receipt. The committed harness `tools/matrix_integration.py` covers them with
these checks, all against a disposable Synapse and a deterministic fake OpenRig
transport:

| Check | What it proves |
|---|---|
| `steering-modes-and-batch` | `queue`, `steer`, and `auto` behavior; one ordered same-thread batch keeps every source event ID and the canonical thread root. |
| `steering-fallback-restart` | Refused injection and crash-after-injection recovery: rescan before fallback, one durable fallback attempt only when the seat is idle without admission. |
| `steering-late-admission` | Input that materializes as the next turn is adopted without resending; the answer keeps the follow-up source ID and the completed root's thread, and typing and `arbeitet` status continue until the queue drains. |
| `acceptance-reaction` | With the optional receipt enabled, exactly one bridge `m.annotation` 👍 on each accepted owner event, including batch members; a crash after the send leaves the acknowledgement pending and restart reuses the persisted transaction; an untrusted room member gets no reaction, no acceptance record, and no OpenRig prompt. |
| `acceptance-reaction-default-off` | With the receipt off (default), an accepted owner message is queued, typing is observed, it is answered and its qitem is done, and it gets no bridge reaction and no acceptance record. |

The report also carries a `steering_latency` section (median and maximum
milliseconds per stage and kind, with `method` and `boundary` strings). It
measures the deterministic transport, not live Codex.

The bounded terminal state for a seat that stays busy without admission
(`openrig.response_timeout_minutes`, recorded in `steering_failures`) is
covered at the unit level in `tests/test_steering.py`; the Matrix harness does
not wait out that timeout.

**Not proven:** live Codex evidence. The cost directive for this cell forbade
live Codex turns, so no run shows a real Codex terminal retaining, queueing, or
admitting pane input. Exactly-once delivery after steering admission fails is
not claimed; a late pane admission remains a documented best-effort residual
risk because OpenRig has no acknowledged safe-boundary or turn-admission
primitive. The source SHA, check count, and retained report for the final
candidate (`final-0.12.0-integration-report.json`) are recorded in
[`docs/artifacts/README.md`](artifacts/README.md).

### Issue #39 duplicate activity-card evidence

**Observed.** A Matrix run of PR #38 at `aca64fb` (based on `release/0.11.0`)
failed `threaded-prompt-restart` with two initial `**Aktivität:** Bereit` cards
for one request, 0.79 s apart; only the second was ever edited.

**Cause: the initial card send was not idempotent.** At `aca64fb` the first
activity-card message was sent without a transaction ID, so `matrix-nio` used a
random one for every attempt. When Matrix stored the card but the bridge never
saw the response (a timeout or reset that `_deliver_to_request` turns into a
failed attempt, or a SIGTERM restart mid-request), the bridge did not own the
card and the next poll sent a new one. That retry got a new random transaction,
so Matrix created a second card; the bridge recorded and edited only that one.
The event-ID ownership path was not the defect: the card's event ID is saved
under the agent lock as soon as a response arrives.

**Fix on `main`.** `f34831f` (#34) sends the initial card with the stable
transaction ID `activity-card-<request id>`. The pinned Synapse deduplicates
`PUT /send/{type}/{txnId}` per user and device, both in memory and through its
persisted `event_txn_id_device_id` table, so a retry returns the card that
already exists. `aca64fb` is not an ancestor of `main`; no `main`-based run
reproduced the failure.

**Evidence.**

- Retained Synapse databases of 28 Matrix runs on this host on 2026-10-25
  (main-based sources, hundreds of requests with cards): no request created
  more than one card (`event_txn_id_device_id` joined to `event_json`,
  excluding `m.replace` edits).
- `tests/test_activity_card.py::test_lost_card_response_is_retried_into_the_same_card`
  runs `_poll_agent` against a fake room with Synapse's transaction semantics;
  the first card send is stored but its response is lost. It passes on `main`
  and fails with two cards when the stable transaction ID is removed (the
  `aca64fb` behavior).
- `activity-card-crash-recovery` (Matrix harness, real Synapse): the bridge
  exits right after Matrix accepted the initial card and before recording its
  event ID (`OPENRIG_BRIDGE_TEST_CRASH_AFTER=activity_card_send`), restarts,
  and must end with exactly one card that every edit targets, one answer and
  one completed qitem. With the stable transaction ID removed, the check
  failed exactly like the original report (two cards, only the second edited:
  `cards=[$trsRzy…, $nR9Lk2…], edits=[$nR9Lk2…]`).

**Remaining timing boundary.** Deduplication holds while the retry uses the
same bridge device within Synapse's 24-hour transaction-ID retention. A retry
after more than 24 hours, or after the bridge logs in as a new device, could
still create a second card. A terminal edit that keeps failing posts one
explicit terminal fallback message by design
(`test_terminal_activity_edit_failure_posts_explicit_terminal_fallback`); that
message is a final status, not a second active card.

### Issue #4 hybrid steering evidence (0.11.0 candidate, historical)

Issue #4 source `774d1038b61f79fbf28c6709a8d7c5dfbc8dee99` passed 277 unit
tests. Three consecutive exact-source disposable-Synapse runs passed
(`evidence_valid: true`, 22/22 checks in 92.7s, 102.0s, and 106.4s). The runs
covered queue/steer/auto behavior, one ordered
same-thread batch with every source event ID and canonical root, top-level and
cross-thread FIFO isolation, one answer with source-event metadata, qitem
closure, refused-injection fallback, crash-after-injection restart recovery,
completion-race adoption without resend, and rejection of an unrelated human
transcript turn. Multi-qitem terminal recovery preserves known terminal members,
fail-closes remaining siblings, and removes the batch only after one durable
notice. `finishing` and `failing` reconciliation accepts every remote terminal
state before retrying a contradictory local transition. Unconfirmed steering
gets a bounded asynchronous transcript-rescan grace period before fallback. The
grace also covers restart-from-`injecting` and is refreshed before timeout or
delivery-failure removal. Legacy pending requests without an `id` are removed by
object identity and retain stable `event_id`-based failure transaction IDs. The
worktree was clean for the tested SHA. The retained final run's report is
[`issue-4-integration-report.json`](artifacts/issue-4-integration-report.json).

Initial activity-card sends now carry the stable
`activity-card-<request-id>` Matrix transaction ID, so a slow or ambiguous send
response cannot create a second card on retry.

This evidence supports only the narrowed best-effort contract. The fake
transport models pane-style admission under an explicit test switch; it does
not prove that a live Codex runtime accepts input at a model/tool boundary.
OpenRig exposes no admitted-turn or safe-boundary acknowledgement.

## Documentation checks

Check every relative Markdown link from the repository root:

```bash
python3 tools/check_markdown_links.py
```

Check Markdown structure with the repository's narrow lint policy:

```bash
npm ci
npx --no-install markdownlint-cli2
```

Run the artifact redaction check from the
[redaction checklist](#redaction-checklist), and inspect binary artifacts
manually.

## Integration boundary

The engine slice provides `tools/matrix_integration.py`. It starts disposable
Docker Synapse and drives the bridge as a subprocess through a deterministic
OpenRig CLI fixture. The final report is
[`final-0.11.0-integration-report.json`](artifacts/final-0.11.0-integration-report.json)
(schema version 1).

The observed run used:

```bash
.venv/bin/python tools/matrix_integration.py \
  --timeout 60 \
  --source-commit 3b21b72330a9b0810457a3ee4d11fdd2c144ae90
```

It reported `status: pass` with 20 checks at
`2026-10-24T22:30:43Z` in 49.9 seconds, with
`target.source.commit` equal to the tested source SHA and
`target.source.worktree_clean: true`. The digest-pinned Synapse image is
recorded in the report. The owner client was a real `matrix-nio` client; the
homeserver was loopback-only; the OpenRig transport was fake.

The harness boundary intentionally does not prove:

- production OpenRig daemon behavior or queue recovery;
- a live Codex model turn;
- local Parakeet recognition quality;
- Element rendering or human SAS/cross-signing.

Those are manual checks or explicit release gaps, not mocked successes. The
redacted evidence excerpts are in `docs/artifacts/`.

## Required live/manual matrix

Keep these rows open until evidence is actually committed:

| Scenario | Expected observation | Evidence |
|---|---|---|
| Setup and doctor | Private config is created; token presence and Matrix `whoami` are checked without exposing the token. | Pass on disposable Synapse; `setup-doctor.txt` |
| Agent room creation and invite | Private encrypted room is created once and the owner receives an invite. | Pass on disposable Synapse; `room-status.txt` |
| Ordinary chat | Prompt reaches the same configured seat; typing/status change; final answer arrives in a thread. | Matrix path pass with fake OpenRig; live Codex remains manual; `ordinary-chat.txt` |
| Live activity | The editable card shows a runtime-derived activity label and sanitized commentary, then a terminal label; the final answer stays separate. | Pass on disposable Synapse; fine-grained activity depends on OpenRig's coarse signal |
| Threaded prompt | Prompt already in `m.thread` keeps its canonical root across commentary, final answer, a bridge restart, and qitem closure. | Pass on disposable Synapse with fake OpenRig; `threaded-prompt-restart` |
| FIFO queue | Second prompt waits; pinned count updates; answers remain ordered. | Pass with fake transport; `ordinary-chat.txt` |
| Status and typing | Topic switches `bereit`/`arbeitet`; typing is visible while working. | Pass on disposable Synapse; `room-status.txt` and `ordinary-chat.txt` |
| Image exchange | Incoming encrypted image is decrypted to the media inbox; outgoing image is encrypted. | Pass byte-for-byte on disposable Synapse |
| Arbitrary-file exchange | PDF and unknown binary preserve bytes, filename, MIME type, size, safe storage, and encrypted Matrix events; restart recovery delivers once. | Automated Synapse harness covers both directions and pending outbox recovery |
| Voice note | The voice pipeline produces one threaded status message edited to the transcript and forwards the prompt once. No-speech, failure, edit rejection, initial-send failure, and crash recovery have durable terminal paths. | Deterministic transcriber and Matrix edit lifecycle pass on disposable Synapse; real Parakeet recognition and Element rendering remain gaps. |
| Decision/poll | Brief and poll are pinned; selection is returned; pinned event is cleared. | Pass on disposable Synapse; Element rendering remains manual |
| Reaction feedback | Owner reaction reaches the agent but produces no visible bot answer. | Pass with fake transport on disposable Synapse |
| Restart/recovery | Accepted state and room mapping survive restart; active queue behavior is reported honestly. | Pass on disposable Synapse; report |
| Device SAS verification | On a trusted physical device signed in as the owner (Element Desktop or Element X), open the bot user, choose device `OPENRIG_BRIDGE` (or the configured bot device), start **Verify by emoji**, compare the seven emojis the bridge posts, click **They match**, and type `ja`; repeat on a second device confirming in the other order. Record the bridge message `Gerät … wurde erfolgreich verifiziert.`, `openrig-matrix-bridge doctor` listing the device, and whether the room's unverified-device warning disappears for that session. Element still showing the account as not verified by its owner is expected (no cross-signing). | Manual evidence absent; Element Web reported verification unavailable on 2026-10-24; human-only gate in issue #1 |
| Element X threaded reply | Owner observes the prompt and bridge answer in the same thread. | Manual owner report on 2026-10-24; screenshot pending; matrix protocol behavior covered separately |
| Element Web conversation and queue | Owner prompt, activity card, final answer, and pinned queue card render as documented. | Screenshot-backed synthetic harness evidence; see [`screenshots/README.md`](screenshots/README.md) |

## Unsupported or unverified claims

The following must not be presented as proven today:

- “Verified with Element” unless a real device SAS flow is recorded.
- “Cross-signed” or “automatically trusted on every device.”
- “Fully tested end to end” while coverage is unit tests plus mocks.
- “Any Codex conversation from any device” without checking device trust and
  the one-owner allowlist.
- “Voice works with any audio format or language at production quality.”
- “Messages are invisible to the homeserver.” E2EE protects content, while
  Matrix metadata remains visible to the homeserver.
- “Zero downtime” or “restart-safe” without the restart evidence above.

## Redaction checklist

Review every artifact before commit:

```bash
rg -n -i \
  'access[_ -]?token|password|secret|recovery[_ -]?key|private[_ -]?key|@[^ ]+:[^ ]+|![^ ]+:[^ ]+|mxc://[^ ]+' \
  docs/artifacts
```

The check should produce only intentional synthetic values such as
`@owner:matrix.example.org`. Inspect images manually because text search cannot
detect a room name drawn in a screenshot or a notification preview.

Do not retain raw `state.json`, Matrix crypto stores, user/device keys, access
tokens, password-manager output, private homeserver logs, or unredacted
timeline exports.

# Changelog

## 0.12.1

- The queue card is pinned only while at least one request waits behind the
  active one. A single active request no longer leaves a pinned
  "0 Nachrichten warten" card; the card is unpinned as soon as nothing waits,
  pinned again in place when a request starts waiting, and a stale card is
  unpinned at startup. Silent feedback never creates a card
  ([#86](https://github.com/ssalentin/openrig-matrix-bridge/issues/86)).

## 0.12.0

Release tracker: [Issue #46](https://github.com/ssalentin/openrig-matrix-bridge/issues/46).
All pull requests of the milestone are merged.

### Steering and follow-ups (#4, #36)

- Add best-effort same-thread steering with `queue`, `steer`, and default
  `auto` modes while preserving one active Codex turn per seat.
- Persist each injected batch and transcript boundary before pane input, require
  transcript turn evidence before admission, and adopt late next-turn
  materialization without resending.
- Keep every source event, qitem, and canonical thread root, producing one
  coherent answer for an admitted same-thread batch.
- End a seat that stays busy without steering admission for
  `openrig.response_timeout_minutes` in a bounded terminal failure recorded in
  `steering_failures`, without resending.
- Do not claim exactly-once delivery after steering admission fails; a late pane
  admission remains a documented best-effort residual risk (#36).
- Let the immediate steering admission scan match when the active request has
  no recorded turn id (state saved before turn attribution existed) (#48).

### Acceptance receipt (#46, #48)

- Optional 👍 acceptance receipt, off by default; enable it with
  `openrig.acceptance_reaction_enabled: true`. When enabled it is sent once on
  each durably accepted human message (accepted, not completed) and is
  idempotent across retries and restarts.
- Expire acceptance receipts that are still pending while the receipt is turned
  off, so turning it back on never sends an old 👍 (#48).

### Typing and status (#47)

- Derive typing and the room status from the queue head on every poll and at
  startup: thread replies, steered and late-admitted follow-ups keep typing and
  `arbeitet`, `bereit` is written only when the queue drains, a restarted
  bridge restores typing for in-flight requests, and a SIGTERM stop (systemd
  stop or restart) runs the normal cleanup, clears typing and exits 0 instead
  of leaving it visible for up to 30 seconds.

### Test harness (#52, #50, #65, #78)

- Keep the fake OpenRig transport busy until its turn completes, make the hold
  of "hold first" prompts configurable (`OPENRIG_FAKE_HOLD_SECONDS`), and let a
  late steer materialize only after its source turn has completed, so steering
  probes no longer depend on wall-clock timing (#52). Test tooling only.
- Make the late-steering fake-transport test deterministic: the first turn stays
  busy long enough for a slow-starting steering send, and the test waits for the
  expected answers with a bounded poll that parses only complete transcript
  lines (#50). Tests only.
- Stop the late-steering test from tearing the fake's state file: the test
  replaces the state atomically and waits for the answer with a bounded poll, and
  a regression fails if a poller can see a half-written file; the unrelated-human-
  final test waits with the same bounded poll (#65). Tests only.
- Count only activity-card edits in the `activity-card-crash-recovery` harness
  check, so an in-place edit of the pinned queue card is no longer taken for a
  second activity card (#78). Test tooling only.

### Related messages, galleries and voice (#54, #57)

- Handle Element X `dm.filament.gallery` (and `m.gallery`) albums: every item
  stored in order with explicit per-item failures (#57).
- Deliver related owner messages as one context: thread and reply relations
  first, then top-level events within `openrig.burst_inactivity_seconds` (45)
  and `openrig.burst_max_span_seconds` (300) when one side carries an
  attachment. Later parts join the waiting request, steer into the active turn,
  or arrive as a continuation restating every earlier part; answers are
  threaded under the first part and name every source event (#54).
- Keep source order while a voice note is transcribed or an attachment
  downloads; a later text or file no longer overtakes it.
- Resume an image or file download interrupted by a restart instead of losing
  the event; galleries already resumed per item.
- Add the SHA-256 of every stored attachment, gallery item and preview, and of
  received voice audio, to the agent prompt.

### Queue card (#24)

- Show the pending Matrix prompts in the pinned queue card: the active request
  is distinguished from the ordered waiting entries, each with its source,
  thread marker and a sanitized, truncated preview. The card is edited in place,
  rebuilt after a restart, counts a steered follow-up in the active batch
  instead of as a separate turn, and never shows local paths, secrets or hidden
  output. Files, images and galleries show their filename or caption.
- Fix a request that waited behind a busy seat staying labelled as the next
  request after it started: starting it now refreshes the pinned queue card.
- Behavior change when a final answer cannot be sent: after 3 failed sends the
  bridge fails the qitem, posts a thread notice
  (`Die Matrix-Antwort konnte nicht zugestellt werden: ...`), refreshes the card
  and starts the next request. The request stays at the head of the queue until
  Matrix accepts that notice and is retried on every poll until then; earlier
  builds dropped it silently. Two existing tests were adjusted for this
  (`test_final_delivery_failure_retries_terminal_card_before_advancing` and
  `test_persistent_final_delivery_failure_fails_qitem_and_advances`). The retry
  and hold are unbounded; see Issue #62.

### Agent file delivery (#55)

- Make agent file delivery discoverable and verifiable: `send-file --json`
  prints the Matrix `event_id`, `room_id`, filename, MIME type, size and
  `sha256`; the new [agent file delivery guide](docs/AGENT_FILE_DELIVERY.md)
  states that a local path is never delivery evidence; and the integration
  harness proves a nonzero PDF by returned event ID and decrypted SHA-256 and
  that empty, missing and oversize files fail before upload.

### Reliability and upgrade (#56, #41, #39)

- Reconcile the inbound qitem with `rig queue show` before every finish attempt
  and after a rejected finish update: a row the agent already closed counts as
  finished, with no `note_append_fields_not_admitted` warning and no duplicate
  transition (#56).
- Reconcile pending requests saved by bridge 0.10 at startup, before any claim,
  prompt, notice or answer: a request whose OpenRig qitem is already `done` is
  dropped without replaying its prompt or answer, another terminal state is
  dropped with at most one factual notice, and a still-open request keeps the
  fail-closed ambiguous-prompt behavior. The reconciled phase is saved before
  the request is removed, every pending request is checked (not only the head),
  and a restart during reconciliation is idempotent. A claim is also no longer
  attempted for a qitem whose state is unknown (#41).
- Prove and guard the single activity card after a lost initial send (#39): the
  duplicate seen on the 0.11 line came from a card send without a stable
  transaction ID, which `main` already sent as `activity-card-<request>`. This
  release adds the `activity-card-crash-recovery` harness check, a unit
  regression and an environment-gated fault point; production behavior is
  unchanged.
- Serialize decision publication and deduplicate by qitem ID, so the automatic
  human-queue mirror and a manual decision command cannot publish the same
  brief and poll twice (#80).
- Redact sensitive parts of activity commentary span by span instead of
  replacing the whole message: local paths, tokens, authorization headers,
  cookies, URI credentials and fenced output get category markers such as
  `[lokaler Pfad]` and `[Token entfernt]`, and the progress text stays visible.
  Queue-card previews use the same redaction (#70).

### Setup (#37)

- `setup` checks `MATRIX_ACCESS_TOKEN` with the homeserver `whoami` endpoint
  before writing anything and takes the bot user ID and device ID from it,
  printing a note when they differ from values you supplied. The token is read
  only from the environment and is never accepted as an argument, printed, or
  written to the configuration.
- Omitted `--homeserver`, `--user-id`, `--device-id` and `--allowed-user` are
  prompted for unless the token supplies them, each with a short note on where
  to find it in a Matrix client.
- `setup` stops with a next step when the configuration already exists
  (`--replace-config`), a bridge is already running for the state directory, the
  token is invalid, expired or forbidden, the homeserver is unreachable, `rig` is
  missing or unresponsive (`--rig-command`), no Codex seat is running or a seat
  name is duplicated, the OpenRig human registry is unavailable
  (`--no-human-registration`), or the voice model download fails after the
  configuration is written (`--skip-model`).
- A file lock on the state directory (`bridge.lock`) keeps a second bridge from
  starting on the same state.
- `doctor` reports an invalid token with the same create-a-new-token next step
  and warns when the token belongs to a different user or device than the
  configuration.

### Device verification (#1)

- Complete a device verification when the owner typed `ja` after Element had
  already confirmed, instead of waiting for a MAC that had arrived and later
  reporting a timeout.
- Report a SAS that matrix-nio canceled on its MAC (for example
  `m.key_mismatch`) as failed, with the code, instead of leaving it pending;
  decline a verification request without the emoji comparison with an
  explanation.
- `doctor` lists the owner devices the bridge has verified and the latest
  verification outcome; `setup` prints the verification next step.
- Document per-device trust and keep `matrix-nio` below 0.26; see
  `docs/CROSS_SIGNING.md` for the 2026-10-25 evaluation. A SAS on a real device
  has not been recorded; it remains a human-only gate (Issue #1).

### Documentation (#42)

- Reframe the README as a product landing page, with the detailed guides linked
  from it (#43).

## 0.11.0

- Add encrypted arbitrary-file attachments in both directions, including
  markerless audio routing, private admission snapshots, original metadata,
  safe generated storage paths, and restart-safe outbound delivery.
- Edit each voice-note status message into the local transcript or an explicit
  terminal failure instead of posting a separate progress transcript.
- Preserve canonical Matrix thread roots for text, image, generic-file and
  voice prompts, including prompts that are already thread replies.
- Harden restart recovery for prompts, answers, attachments and OpenRig qitems;
  requests whose prompt-send effect became ambiguous fail closed instead of
  being replayed.
- Reject unsafe Matrix endpoints, unsafe agent path components and credential
  material in URLs.
- Redact activity-card content and bound commentary, terminal-card and
  delivery-failure recovery.
- Add accessible setup documentation, the Relay symbol and theme-safe avatar
  assets while retaining the OpenRig Matrix Bridge product name.
- Add deterministic documentation, link, package, clean-install and exact-source
  Matrix release gates.
- Add an operator runbook for candidate installation, single-instance checks,
  live attribution smoke tests, rollback and exact artifact provenance.

## 0.10.0

- Add default `auto`, explicit `steer`, and strict FIFO `queue` modes for
  short same-thread follow-ups.
- Batch every source event and qitem under the canonical thread, use transcript
  turn identity to reject unrelated human answers, and fall back losslessly when
  best-effort pane injection is unavailable or ambiguous.
- Treat an owner leaving or rejecting an agent room as a durable opt-out instead
  of recreating the room on restart.

## 0.9.0

- Pass Matrix reactions to the responsible agent as silent feedback without
  creating visible replies or inbound queue items.

## 0.8.1

- Restore the complete unique ghost avatar pool and the dedicated OpenRig
  profile avatar.

## 0.8.0

- Create priority-aware OpenRig queue items for accepted Matrix messages and
  track their claim and completion lifecycle.

## 0.7.1

- Refine the generated avatar silhouettes so each pool entry is visually
  distinct.

## 0.7.0

- Add automatic, persistent, collision-free avatar assignment for agent rooms
  and the bridge profile.

## 0.6.0

- Add 32 original OpenRig agent avatars with deterministic collision-free assignment.
- Preserve custom room avatars by default and provide explicit avatar replacement.

## 0.5.0

- Replace the Moshi gateway with direct OpenRig seat transport.
- Resolve Codex transcripts from the live OpenRig node inventory.
- Deliver Matrix prompts with `rig send --raw` and no separate chat gateway.
- Mirror new OpenRig human updates and decisions into the source agent's room.
- Register the Matrix owner as an OpenRig human recipient during `setup`.
- Mirror visible Codex commentary into one editable Matrix status message per request.
- Reduce active transcript and outbox polling latency to 200 ms.
- Poll OpenRig human-queue handoffs every two seconds by default.
- Show pending Matrix prompts in one editable pinned queue card.

## 0.4.0

- Render agent Markdown as native Matrix formatted messages, including tables.
- Receive encrypted PNG, JPEG, GIF, and WebP images into a per-agent inbox.
- Add `send-image` for encrypted image delivery from an agent to its room.
- Add `send-message` for explicit formatted agent messages into Matrix.

## 0.3.0

- Add pinned OpenRig decisions and encrypted multiple-choice polls.
- Return Matrix poll selections to the responsible agent as queued prompts.
- Name Matrix rooms after the agent role only; the bridge profile is `OpenRig`.

## 0.2.0

- Add per-agent Matrix live status through stable room topics and structured
  room state, without changing the room name or generated avatar.
- Show a persistent typing indicator while an agent is working.
- Add NVIDIA Parakeet TDT 0.6B v3 voice transcription with automatic model
  download and checksum verification.
- Add Matrix emoji SAS device verification.
- Add `setup`, `doctor`, and `download-model` commands.

## 0.1.0

- Initial OpenRig agent-to-Matrix bridge.

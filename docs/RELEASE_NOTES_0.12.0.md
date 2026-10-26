# OpenRig Matrix Bridge 0.12.0

OpenRig Matrix Bridge 0.12.0 adds best-effort steering for eligible follow-ups
without starting a second agent or allowing concurrent Codex turns. It also
delivers related messages (a gallery and the text about it, a voice note and the
photo after it) to the agent as one context, shows the queue in a pinned card,
makes agent file delivery explicit and verifiable, and makes setup and the
upgrade from 0.10 safer.

All pull requests of the milestone are merged.

## What changed

### Steering and follow-ups

- `queue`, `steer`, and default `auto` modes are supported.
- `auto` steers only short follow-ups in the exact active Matrix thread, and
  parts of a related exchange (below) whatever their length. Other top-level
  prompts and prompts in another thread remain separate FIFO work.
- Rapid same-thread follow-ups form one ordered batch. Every Matrix event keeps
  its source ID and OpenRig qitem, and the batch is answered once under the
  canonical thread root.
- The bridge persists the exact transcript offset and injected text before pane
  input. Pane paste alone is never treated as delivery.
- Admission requires a matching transcript user message and turn. The bridge
  rescans from the persisted boundary, waits while the seat remains busy, and
  adopts input that Codex materializes as the next turn without resending it.
  State saved before turn attribution existed is admitted on the immediate scan.
- A batch makes one durable fallback attempt as a normal queued turn only after
  the runtime is idle and matching admission is still absent.
- A seat that stays busy without admission for `openrig.response_timeout_minutes`
  ends in a bounded terminal timeout: the thread shows
  `Steering-Zeitüberschreitung`, `state.json` records the batch under
  `steering_failures`, and the input is not resent.

### Acknowledgement, typing and status

- An optional 👍 acceptance receipt, off by default, is enabled with
  `openrig.acceptance_reaction_enabled: true`. When enabled, each durably
  accepted human message gets exactly one 👍 (`m.annotation`), sent after the
  request is saved and queued or joined to a steering batch. It means accepted,
  not finished; the final answer remains the completion signal. Retries,
  restarts, duplicate polls, and batch members never duplicate it, and
  untrusted, ignored, duplicate, or rejected events get none. A 👍 that was
  still pending when the receipt was turned off is expired instead of being sent
  if the receipt is turned on again later.
- When the receipt is off, the status card and typing indicator are the live
  acknowledgement (see Known limits). Typing and the room status follow the
  queue head: they continue through steered and queued follow-ups, `bereit` is
  written only when the queue drains, a restarted bridge restores typing for
  in-flight requests, and a SIGTERM stop (systemd stop or restart) clears typing
  and exits 0.

### Related messages, galleries and voice

- Related owner messages reach the agent as one context. A thread or reply
  relation links parts without an inactivity limit, within
  `openrig.burst_max_span_seconds` (300 s) of the exchange's first part (a later
  thread reply is answered in the thread as a new exchange); top-level events
  from the same owner link while each gap stays within
  `openrig.burst_inactivity_seconds` (45 s) and the whole exchange within
  `openrig.burst_max_span_seconds` (300 s), when the new event or the previous
  one carries an attachment. Two plain texts never link. A later part joins the
  waiting request, steers into the active turn, or arrives as a continuation
  that restates every earlier part, so a gallery and the text written 30 s later
  are answered together in the gallery's thread.
- Source order holds while a voice note is transcribed or an attachment
  downloads, interrupted image and file downloads resume after a restart, and
  every stored attachment, gallery item and preview is listed with its SHA-256.
- Element X galleries (`dm.filament.gallery`, `m.gallery`) are stored item by
  item in order, with explicit per-item failures.

### Queue card

- The pinned queue card lists the active request and the ordered waiting
  requests with source, thread marker and a sanitized preview. See
  [The queue card](USER_GUIDE.md#the-queue-card). Starting a request that waited
  behind a busy seat now refreshes the card.
- Changed behavior: a final answer that fails to send three times now posts a
  thread failure notice and keeps the request at the head of the queue until
  Matrix accepts that notice; earlier builds dropped it silently. See Known
  limits.

### Agent file delivery

- An agent that generates a PDF or other file for the owner delivers it with
  `send-file --json` and quotes the returned Matrix event ID and SHA-256; a local
  path is never delivery evidence. See the
  [agent file delivery guide](AGENT_FILE_DELIVERY.md).

### Reliability and upgrade

- Before finishing an inbound qitem the bridge reads its state. A qitem the
  agent already closed is treated as finished instead of being updated again,
  which OpenRig rejects as `note_append_fields_not_admitted`.
- Upgrading from 0.10 with requests still pending: at startup the bridge reads
  each request's OpenRig qitem first. Requests whose qitem is already closed are
  removed without resending the prompt or answer (a canceled, failed or denied
  qitem produces one notice), instead of repeating claim errors.
- The activity card is sent with the stable transaction ID
  `activity-card-<request>`, so Matrix returns the existing card when a send is
  retried after a lost response or a restart. The duplicate seen on the 0.11 line
  came from a send without it; this release adds the proof and a guard, not a
  behavior change.
- Human decisions are serialized and deduplicated by qitem ID, preventing the
  automatic human-queue mirror and a manual `decision` command from posting the
  same brief and poll twice.
- Activity commentary keeps its progress text: each local path, token,
  authorization header, cookie, URI credential or fenced output is replaced by a
  marker such as `[lokaler Pfad]` or `[Token entfernt]`, instead of the whole
  message becoming `[redacted unsafe commentary]` (#70). Queue-card previews use
  the same redaction.

### Setup

- `setup` validates `MATRIX_ACCESS_TOKEN` with the homeserver before writing
  anything, takes the bot user ID and device ID from it, and prompts only for
  what the token does not supply. It stops with a next step for an existing
  configuration (`--replace-config`), a bridge already running on the state
  directory, an invalid or expired token, an unreachable homeserver, a missing
  `rig`, no running Codex seat or a duplicate seat name, an unavailable OpenRig
  human registry, or a failed voice model download. `doctor` reports an invalid
  token with the same next step. See the
  [installation guide](INSTALLATION.md#what-setup-infers-and-what-you-provide).

### Device verification

- A verification completes when you type `ja` after Element already confirmed; a
  SAS that matrix-nio cancels on its MAC (for example `m.key_mismatch`) is
  reported as failed with the code; a request without the emoji comparison is
  declined with an explanation. `doctor` lists the owner devices the bridge has
  verified and the latest outcome. Trust is per Element device: the bridge does
  not support cross-signing (see [Cross-signing evaluation](CROSS_SIGNING.md)).

### Test harness

- The fake OpenRig transport stays busy until its turn completes and late steers
  materialize only after their root turn, so the steering probes no longer
  depend on wall-clock timing. The late-steering unit test waits for its
  expected answers instead of reading the transcript on a fixed deadline.
- The late-steering fake-transport tests replace the fake's state file atomically
  and wait for the expected answers with a bounded poll, so a torn read can no
  longer kill the detached worker and fail the test (#65).

## Proven and NOT PROVEN

### Proven

Proven means shown by an automated check against a disposable Synapse with a
deterministic OpenRig transport, or by unit tests: every pull request passed CI
(test, docs, artifacts, matrix-integration) and an independent QA gate whose
exact-source harness runs were `evidence_valid`. Most of those runs were locked
against other host load. The runs for three changes (the queue card, file
delivery and finish reconciliation) were not, and their raw reports were lost in
a sandbox restore, so those results are recorded observations (finish
reconciliation also has one locked diagnostic run that passed); that is why the
final candidate is re-run locked and its report retained. The claim-by-claim list
is in
[Verification and evidence](VERIFICATION.md); the retained report for the final
candidate is `final-0.12.0-integration-report.json`: the third of three
sequential locked exact-source runs at `d999efb`, 37 of 37 checks passed,
`evidence_valid: true`, `worktree_clean: true`. The wheel and sdist of that
candidate have the SHA-256 values in
[Artifacts](artifacts/README.md), verified with `tools/verify_release.py` and a
clean-venv wheel install.

- Steering admission, batching, fallback and restart recovery against the
  transcript the fake transport writes.
- The 👍 receipt, typing and status behavior, related-message and gallery
  delivery, the queue card, and the failure-notice behavior above.
- `send-file --json` end to end: a nonzero PDF is received by the exact returned
  event ID, decrypts to the reported SHA-256, and empty, missing and oversize
  files fail before upload.
- Finish reconciliation against the installed `rig` command's update syntax, and
  the reconciliation of 0.10-shaped pending requests.
- The single activity card after a lost send, and the `setup` and `doctor`
  failure messages.
- One brief and one poll per qitem when the automatic human-queue mirror and a
  manual `decision` command race, against a real Synapse and bridge process.

### NOT PROVEN

- **Live Codex steering.** No live Codex turns were run, so the latency of a real
  Codex terminal retaining, queueing or admitting pane input is not measured.
  The latency table in the [runbook](RUNBOOK_0.12.0.md) is empty, and the
  deterministic-transport figures in the retained report are not live latency.
- **Device SAS on a real device.** No SAS flow with a real Element or Element X
  device has been recorded, and neither has whether Element stops showing its
  unverified-device warning afterwards.
- **Element X on a real device.** Thread and queue-card rendering were captured
  in Element Web against a disposable Synapse
  ([Verification and evidence](VERIFICATION.md)); the typing indicator, the queue
  card and galleries were not checked in Element X on a real device.
- **A real 0.10 state file.** The reconciliation is tested with a fixture
  reconstructed from the 0.10.0 request shape, not a state file from a running
  0.10 bridge.
- **A Matrix outage.** The failure-notice hold has no time limit (Issue #62) and
  has not been exercised against a real prolonged outage.

### Human-only gates

These cannot be closed by the development cell and are for the Engineering Lead
or owner:

1. The real-device SAS check in [Verification and evidence](VERIFICATION.md)
   (Issue #1): record the bridge message, `doctor` output and whether the warning
   disappears.
2. The disposable-seat live Codex check, runbook section 5, before production
   traffic.
3. A check in a real Element X client of the typing indicator, the queue card and
   a gallery.

## Runtime boundary

OpenRig pane delivery does not expose an acknowledged safe model/tool boundary
or an admission-turn token. This release therefore does not claim guaranteed
mid-turn interruption, a guaranteed application point, or proof that every
future runtime will expose a transcript record at the same time.

The bridge preserves the complete source batch until it adopts transcript
evidence, makes its one durable fallback attempt, or records a terminal failure.
Exactly-once delivery is not claimed after admission cannot be proven: if the
pane retains input and Codex admits it after the fallback deadline, the runtime
can see it twice. This late-admission case is a documented best-effort residual
risk. The bridge maintains one sequential Codex turn per seat.

## Upgrade

Use the [0.12.0 installation and rollback runbook](RUNBOOK_0.12.0.md). Back up
the configuration and complete state directory before replacing the service.
Requests still pending from a 0.10 bridge are reconciled against OpenRig at
startup, as described above.

## Known limits

- The failure-notice hold described above has no time limit: if Matrix keeps
  rejecting every send for a room, that agent's later requests wait behind the
  failed one. Tracked in Issue #62.
- Matrix typing and the room status are room-wide: `m.typing` has no thread
  field (element-meta#1495), so clients may show the typing indicator only in
  the main room timeline, not inside a thread view.
- Events delivered during a restarted bridge's initial sync are not routed: the
  message is not delivered and, with the receipt enabled, gets no 👍, which
  makes the gap visible to the owner. This predates 0.12.0.
- Timing correlation is a heuristic: an unrelated top-level message sent
  within the inactivity boundary before or after a voice note, image, file or
  gallery joins that exchange. Use a thread to keep it apart, or set
  `openrig.burst_inactivity_seconds: 0` to turn timing correlation off.
- A related part waits for earlier parts of its exchange that are still
  downloading or being transcribed; there is no separate timeout beyond the
  download and transcription failure paths.
- Matrix device SAS and complete cross-signing remain tracked by Issue #1.
- Commentary redaction (#70) does not recognise three ambiguous prose shapes:
  an unquoted value of several words after a credential label
  (`password: two words secret` replaces only `two`); a letter used as a
  quotation mark (for example U+A71D), which is read as ordinary words; and a
  quoted value more than one word after a bare "cookie"
  (`cookie word more "…"`), since a header value starts right after the name,
  a colon or an equals sign. Any real Cookie, Set-Cookie or Authorization
  header, bearer or Git token, URI userinfo and fenced output is redacted.
  These and the other boundaries in the [user guide](USER_GUIDE.md) are
  tracked in Issue #84.
- Commentary redaction does not recognise JSON-quoted secret values (for example
  `"password": "..."`) or PEM private-key bodies, so they appear unchanged in the
  activity card and in queue-card previews. This predates 0.12.0 and is tracked in
  Issue #84.
- A retry of the initial activity card more than 24 hours later (Synapse's
  transaction-ID retention) or from a new bridge device could still create a
  second card.

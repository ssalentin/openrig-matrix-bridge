# OpenRig Matrix Bridge 0.12.0

OpenRig Matrix Bridge 0.12.0 adds best-effort steering for eligible follow-ups
without starting a second agent or allowing concurrent Codex turns.

## What changed

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
- A batch makes one durable fallback attempt as a normal queued turn only after
  the runtime is idle and matching admission is still absent.
- A seat that stays busy without admission for `openrig.response_timeout_minutes`
  ends in a bounded terminal timeout: the thread shows
  `Steering-Zeitüberschreitung`, `state.json` records the batch under
  `steering_failures`, and the input is not resent.
- An optional 👍 acceptance receipt, off by default, is enabled with
  `openrig.acceptance_reaction_enabled: true`. When enabled, each durably
  accepted human message gets exactly one 👍 (`m.annotation`), sent after the
  request is saved and queued or joined to a steering batch. When it is
  off, the status card and typing indicator are the live acknowledgement (see
  Known limits). It means
  accepted, not finished; the final answer remains the completion signal.
  Retries, restarts, duplicate polls, and batch members never
  duplicate it, and untrusted, ignored, duplicate, or rejected events get none.
- The pinned queue card lists the active request and the ordered waiting
  requests with source, thread marker and a sanitized preview. See
  [The queue card](USER_GUIDE.md#the-queue-card). Starting a request that waited
  behind a busy seat now refreshes the card.
- Changed behavior: a final answer that fails to send three times now posts a
  thread failure notice and keeps the request at the head of the queue until
  Matrix accepts that notice; earlier builds dropped it silently. See Known
  limits.
- Related owner messages reach the agent as one context. A thread or reply
  relation links parts without an inactivity limit, within
  `openrig.burst_max_span_seconds` (300 s) of the exchange's first part (a later
  thread reply is answered in the thread as a new exchange); top-level events
  from the same owner link while
  each gap stays within `openrig.burst_inactivity_seconds` (45 s) and the whole
  exchange within `openrig.burst_max_span_seconds` (300 s), when the new event
  or the previous one carries an attachment. Two plain texts never link. A
  later part joins the waiting request, steers into the active turn, or arrives
  as a continuation that restates every earlier part, so a gallery and the text
  written 30 s later are answered together in the gallery's thread.
- Source order holds while a voice note is transcribed or an attachment
  downloads, interrupted image and file downloads resume after a restart, and
  every stored attachment, gallery item and preview is listed with its SHA-256.
- Element X galleries (`dm.filament.gallery`, `m.gallery`) are stored item by
  item in order, with explicit per-item failures (PR #57).
- An agent that generates a PDF or other file for the owner delivers it with
  `send-file --json` and quotes the returned Matrix event ID and SHA-256; a local
  path is never delivery evidence. See the
  [agent file delivery guide](AGENT_FILE_DELIVERY.md).
- Before finishing an inbound qitem the bridge reads its state. A qitem the
  agent already closed is treated as finished instead of being updated again,
  which OpenRig rejects as `note_append_fields_not_admitted`.
- Upgrading from 0.10 with requests still pending: at startup the bridge reads
  each request's OpenRig qitem first. Requests whose qitem is already closed are
  removed without resending the prompt or answer (a canceled, failed or denied
  qitem produces one notice), instead of repeating claim errors.
- The hybrid steering implementation is integrated with the current setup,
  attachments, voice, activity-card, restart, and release documentation from
  `main`.

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

## Known limits

- The failure-notice hold described above has no time limit: if Matrix keeps
  rejecting every send for a room, that agent's later requests wait behind the
  failed one. Tracked in Issue #62.
- Matrix typing and the room status are room-wide: `m.typing` has no thread
  field (element-meta#1495), so clients may show the typing indicator only in
  the main room timeline, not inside a thread view. Element X rendering on a
  real device is not verified.
- Events delivered during a restarted bridge's initial sync are not routed: the
  message is not delivered and, with the receipt enabled, gets no 👍, which
  makes the gap visible to the owner. This predates 0.12.0 and is also present
  on `main`.
- Timing correlation is a heuristic: an unrelated top-level message sent
  within the inactivity boundary before or after a voice note, image, file or
  gallery joins that exchange. Use a thread to keep it apart, or set
  `openrig.burst_inactivity_seconds: 0` to turn timing correlation off.
- A related part waits for earlier parts of its exchange that are still
  downloading or being transcribed; there is no separate timeout beyond the
  download and transcription failure paths.
- Matrix device SAS and complete cross-signing remain tracked by Issue #1.
- Legacy pending-state reconciliation remains tracked by Issue #41.
- The duplicate activity card seen on the 0.11 line (#39) came from an initial
  card send without a stable transaction ID. 0.12.0 sends it as
  `activity-card-<request>`, so Matrix returns the existing card when a send is
  retried after a lost response or a restart; `activity-card-crash-recovery`
  covers that path. A retry more than 24 hours later (Synapse's
  transaction-ID retention) or from a new bridge device could still create a
  second card.
- Live Codex evidence is **not proven** for this release candidate. The
  integration harness uses a real Synapse and a deterministic OpenRig transport,
  so it does not prove delivery into a live Codex terminal. The first live
  observation is the disposable-seat check in the
  [runbook](RUNBOOK_0.12.0.md). Its live-seat latency table is not measured;
  the deterministic-transport median and maximum are in the retained
  `final-0.12.0-integration-report.json`.

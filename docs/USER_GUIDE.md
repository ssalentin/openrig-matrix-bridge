# User guide

This guide covers everyday use of OpenRig Matrix Bridge after installation.
For installation and service management, see
[Installation](INSTALLATION.md). For encryption, device trust, and known
limits, see [Security and trust](SECURITY.md).

## How a conversation works

Each configured agent appears as one private room. The bridge resolves the
existing OpenRig Codex seat, injects your message into that seat's terminal, and
reads the seat's own Codex transcript. It does not start another agent.

The room topic and activity card tell you whether the agent is ready or
working. Normal answers arrive in Matrix in a thread rooted at your prompt.
Multiple prompts remain FIFO behind the active request, and a pinned queue card
shows waiting work.

The default user-facing status text is German:

| Status | Meaning |
|---|---|
| `bereit` | The seat is ready for a prompt. |
| `arbeitet` | The seat is currently working. |
| `Wartet auf Eingabe` | The seat is waiting for your input. |

## Verify the product manually

Use the same owner account listed in `matrix.allowed_users`.

1. Check that the room name is stable and its topic shows `bereit` while the
   Codex seat is idle.
2. Send an ordinary message. The bot should show a typing indicator, the topic
   should move to `arbeitet`, and the final answer should appear in the thread.
3. Send a second message while the first is active. It should remain queued and
   the pinned queue card should count it.
4. Attach a PNG, JPEG, GIF, or WebP image. The agent receives a local file path
   under the configured media inbox.
5. Attach a PDF, archive, office document, audio file, or other generic file.
   The agent receives a generated local path plus the original filename, MIME
   type, and byte count. The original name is metadata, not part of the path.
6. Send a short voice note when transcription is enabled. The bridge creates
   one status reply in the voice-note thread, edits it to the local transcript,
   and forwards the prompt. A rejected edit produces a clearly labeled
   transcript fallback. A restart during processing replaces the stale status
   with an explicit terminal message instead of duplicating the transcript.
   Only `m.audio` events carrying `org.matrix.msc2516.voice` or
   `org.matrix.msc3245.voice` are voice notes; markerless audio follows the
   generic-file path.
7. React to a bridge-authored message. The reaction is forwarded as silent
   feedback and is not mirrored as a visible answer.
8. Publish a decision with the command below and select an option. The choice
   is returned to the responsible agent.

The automated integration harness and its observed boundaries are documented
in [Verification and evidence](VERIFICATION.md). Unit tests alone do not prove
live Matrix delivery.

## Acceptance receipt

When the bridge has durably accepted a message from an allowed user, it adds
one 👍 reaction to that message. The 👍 means *accepted*: the OpenRig queue item
exists and the request is either queued behind the active work or joined to a
steering batch. It does not mean the agent has started or finished. The final
answer in the thread remains the completion signal.

- Every event in a steering batch gets its own single 👍.
- Retries, bridge restarts, and duplicate polls never add a second 👍. The
  reaction uses the deterministic transaction ID `ack-<source event ID>`, and the
  bridge records sent receipts in `acceptance_reactions` in `state.json`.
- Messages from untrusted senders, ignored or duplicate events, and events
  rejected before durable acceptance (for example when the queue item cannot be
  created) get no 👍.
- If Matrix keeps rejecting the reaction, the bridge retries a bounded number of
  times and then records `phase: failed` in `acceptance_reactions`. The request
  itself is still processed and answered; only the receipt is missing.
- Set `openrig.acceptance_reaction_enabled: false` to turn the receipt off. It is
  on by default.

## Follow-ups and steering

`openrig.steering_mode` controls how a follow-up behaves while the seat is
working:

- `queue` keeps all requests FIFO.
- `steer` attempts best-effort delivery for eligible same-thread follow-ups.
- `auto` is the default and uses the same strict eligibility rule.

A follow-up is eligible only when it is in the active request's exact thread,
is no longer than `openrig.steering_max_chars` (2000 by default), and arrives
while the original turn is active. Top-level prompts and prompts in another
thread remain separate FIFO requests. Events received inside
`openrig.steering_batch_window_seconds` (0.5 by default) form one ordered batch.
Every source event keeps its event ID, qitem, and canonical thread root; an
admitted batch receives one answer carrying all source event IDs.

Before pane input, the bridge records the complete batch and the exact
transcript boundary. It adopts steering only after the matching user message and
turn appear in the Codex transcript, and it rescans from that boundary before
any fallback, including after a restart. During
`openrig.steering_admission_timeout_seconds` (2.0 by default) it waits for that
admission. While the seat stays busy it keeps waiting instead of resending, which
covers input that Codex queues and materializes as the next turn. If the seat is
idle and no matching admission exists, the intact batch makes one durable
fallback attempt as a normal queued turn.

If the seat stays busy and admission is never observed, the wait is bounded by
`openrig.response_timeout_minutes` (360 by default). After that the bridge stops
waiting, ends the request in a terminal timeout state, closes its qitems as failed
instead of dropping them, and posts one failure notice in the original thread
that contains `Steering-Eingabe konnte nicht bestätigt werden`. The seat status
reads `Steering-Zeitüberschreitung`. The timeout is measured from the batch's
`injection_attempted_at` and applies only to a batch already typed into the pane. It records the source event IDs, qitem IDs,
thread root, and reason in the `steering_failures` list of `state.json` (the
newest 200 entries are kept). It does not resend the input.

OpenRig currently provides pane delivery, not an acknowledged safe model/tool
boundary or admission-turn token. Steering is therefore best effort. The bridge
does not claim a guaranteed mid-turn interruption point, guaranteed application
before the next turn, or exactly-once delivery after admission cannot be proven.
If the pane retains input and Codex admits it only after the fallback deadline,
the runtime can receive it in addition to the fallback. This late-admission case
is a documented residual risk. Every source request remains durable until one
answer is delivered or an explicit failure is recorded.

## OpenRig queue behavior

Configured agent keys map one-to-one to live Codex sessions. Duplicate sessions
are rejected by configuration validation.

With `openrig.queue_inbound_messages: true`:

- Each accepted owner message creates a real OpenRig queue item for the target
  seat.
- The item uses the configured `inbound_message_priority` (`urgent` by
  default), the `matrix,inbound` tags, and `--no-nudge` because the bridge
  already delivers the prompt.
- The bridge claims the item when work starts and closes it after the Matrix
  answer is delivered.
- Additional Matrix requests remain FIFO behind the active request.
- A single pinned queue-status message is edited as the count changes and
  unpinned when the queue drains.

Prompt and final-answer delivery persist an operation phase before remote
effects. If a bridge exits after starting a prompt but before recording an
acknowledgement, restart fails that request explicitly rather than risking a
duplicate prompt. Final answers retry with a stable Matrix transaction ID, so a
restart can replay the send without creating a second visible event. Qitem
claim, finish, and fail operations reconcile through `rig queue show` after
restart before retrying. Attachment outbox requests use the same stable
transaction rule and are not republished after a recorded delivery.

Qitem lifecycle failures are fail-closed. If qitem creation fails, the bridge
posts an error and does not send the prompt. If claim fails, the request remains
pending with `qitem_phase: claiming` and retries without falsely appearing
active. If finish or fail-close fails, the request remains locally pending and
startup reconciles the qitem with `rig queue show` before retrying or removing
the request.

The bridge identity is `matrix-bridge@kernel` by default. Change
`openrig.sender_session` only if that identity is intentional in your OpenRig
installation. Set `queue_inbound_messages: false` only for installations that
do not want Matrix work represented in the OpenRig queue.

New OpenRig human updates and decisions are mirrored only when their source
agent's configured session matches. The bridge records a watermark on its
first scan and does not replay the prior queue on first start.

## Decisions and polls

Publish a queue-backed decision into the agent's room:

```bash
openrig-matrix-bridge decision \
  --agent advisor \
  --qitem qitem-20261024000000-example \
  --config "$HOME/.config/openrig-matrix-bridge/config.yaml"
```

The command reads the queue item's summary, body, and optional
`humanQuestions`. A written brief is pinned. Structured questions with two to
four choices are published as Matrix polls; the brief and polls are removed
from the pinned set after all structured questions are answered.

## Images, files, and explicit messages

Send an encrypted image into an agent's room:

```bash
openrig-matrix-bridge send-image \
  --agent advisor \
  --path /tmp/screenshot.png \
  --caption 'Screenshot of the current view'
```

Send any other file as an encrypted downloadable attachment:

```bash
openrig-matrix-bridge send-file \
  --agent advisor \
  --path '/tmp/Quarterly report.pdf' \
  --caption 'Reviewed quarterly report'
```

The bridge preserves the source basename and inferred MIME type. Use
`--mime-type application/x-custom` only when the file extension is ambiguous.
Unknown extensions use `application/octet-stream`. Non-image files use Matrix
`m.file`, so clients show them as downloads rather than pretending they are
images.

Send an explicit, formatted message into the room:

```bash
openrig-matrix-bridge send-message \
  --agent advisor \
  --body-file /tmp/report.md
```

These commands use the running bridge's encrypted Matrix state. Plain terminal
conversation is not mirrored automatically. A command that queues work while
the bridge is stopped exits non-zero with the durable request ID; the running
bridge retries it after restart.

## Attachment limits, storage, and recovery

- `media.max_image_bytes` and `media.max_file_bytes` independently cap inbound
  and outbound image and generic-file content. Both default to 26,214,400 bytes
  (25 MiB), and rejection messages include the effective limit.
- Inbound bytes are written atomically under
  `<media.inbox_dir>/<agent-key>/<generated-id>.<safe-suffix>`. The original
  filename is preserved as prompt metadata but is never used as a filesystem
  path. Zero-byte, oversized, corrupt, interrupted, and metadata-inconsistent
  transfers are rejected without publishing a successful file.
- Generic files are stored as `0600` files in `0700` directories. The bridge
  does not automatically delete inbox files or outbound snapshots; apply your
  own retention policy. At `send-file` admission, the bridge rejects symlinks
  and non-regular source paths, opens the selected file without following a
  final symlink, and copies its bytes to a `0600` snapshot under
  `<state_dir>/media-outbox/files`. The request records the source path,
  snapshot path, byte count, and SHA-256. `state.json` never contains file
  bytes, but the snapshot directory does. Protect and redact the complete state
  directory accordingly.
- The bridge account must be trusted to read each explicitly requested source
  path. The bridge does not drop privileges while taking the snapshot. After
  admission, the original source may change or disappear; retries upload only
  the immutable snapshot and verify its recorded size and digest.
- Outbound requests in `<state_dir>/media-outbox` survive restart. A stable
  Matrix transaction ID prevents a retried request from creating a second
  event. Success is recorded only after a Matrix event ID is returned; terminal
  failures move to `media-outbox/failed` and are recorded in
  `media-failures` state.
- Files are not malware-scanned and PDFs or office documents are not rendered
  in preview. Only configured allow-listed owners can send into the room.

See [Security and trust](SECURITY.md) for the encryption and secret-handling
model.

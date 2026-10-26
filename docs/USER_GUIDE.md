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
Multiple prompts remain FIFO behind the active request. While at least one
waits, one pinned queue card shows the active request and the waiting ones in
order (see [The queue card](#the-queue-card)).

The default user-facing status text is German:

| Status | Meaning |
|---|---|
| `bereit` | The seat is ready for a prompt. |
| `arbeitet` | The seat is currently working. |
| `Wartet auf Eingabe` | The seat is waiting for your input. |

The activity card also shows the agent's latest progress commentary. Sensitive
parts are replaced span by span, so the rest stays readable: local paths become
`[lokaler Pfad]`, tokens, keys and authorization headers `[Token entfernt]`,
credentials in a URI `[Zugangsdaten entfernt]`, cookies `[Cookie entfernt]`, and
fenced command output `[Ausgabe entfernt]`. A cookie or authorization header,
and a quoted secret value, is replaced up to the end of its line, so a malformed
or truncated value cannot leave part of it behind. A value that starts with a
quote or any other punctuation counts as quoted and also runs to the end of the
line. A bare "cookie" in prose replaces only the next word, or the rest of the
line when the following word starts with punctuation.

Redaction does not recognise these shapes, which are ambiguous prose rather
than header, cookie or URI credential syntax:

- an unquoted value of several words after a credential label: in
  `password: two words secret` only `two` is replaced;
- a letter used as a quotation mark (for example U+A71D `ꜝ`): such characters
  are letters by definition, so the value they open is read as ordinary words
  (the spacing modifier letters such as `ʼ` are the exception and count as
  quotes);
- a quoted value more than one word after a bare "cookie"
  (`cookie word more "…"`): a header value starts right after the name, a
  colon or an equals sign, so this shape is prose;
- a quote inside a value that starts with a letter or digit
  (`API_KEY=abc"def ghi"`).

The replaced text never reaches the room. Commentary is cut to 600 characters.

## Verify the product manually

Use the same owner account listed in `matrix.allowed_users`.

1. Check that the room name is stable and its topic shows `bereit` while the
   Codex seat is idle.
2. Send an ordinary message. The bot should show a typing indicator, the topic
   should move to `arbeitet`, and the final answer should appear in the thread.
3. Send a second message while the first is active. No queue card is pinned
   while only the first runs; the second should remain queued and a pinned
   queue card should appear and count it.
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

## Optional acceptance receipt

The acceptance receipt is optional and off by default. Set
`openrig.acceptance_reaction_enabled: true` to enable it. Without it, the status
card and the typing indicator are the live acknowledgement.

### Typing and status while work is queued

Typing and the room status follow the head of the queue and are recomputed on
every poll and at startup:

- A reply inside an existing thread shows typing and the `arbeitet` status.
- Steering a follow-up into the active turn refreshes typing.
- A late-admitted thread follow-up keeps typing and `arbeitet` after the root
  answer.
- `bereit` is written only when the queue drains, so there is no `bereit` flash
  or typing gap between queued turns.
- A request in flight across a bridge restart gets its typing indicator back at
  startup.
- Typing stops when the last queued request ends (success, failure or
  timeout). While more work is queued, typing continues into the next turn
  without a gap.
- A `systemctl stop` or restart sends SIGTERM, and the bridge now runs its
  normal cleanup, clears typing and exits 0. Before this change the bridge was
  killed before cleanup and typing stayed visible for up to 30 seconds. A crash
  or SIGKILL still leaves typing up to 30 seconds, the homeserver's typing
  timeout, because no bridge code runs.

Matrix typing and the room status are room-wide: the `m.typing` API has no
thread field (open request: element-meta#1495). Clients may therefore show the
typing indicator only in the main room timeline, not inside a thread view.
Rendering in Element X on a real device is not verified.

When enabled and the bridge has durably accepted a message from an allowed user,
it adds one 👍 reaction to that message. The 👍 means *accepted*: the bridge has durably
accepted it (and created its OpenRig queue item when queueing is enabled) and
the request is either queued behind the active work or joined to a steering
batch. It does not mean the agent has started or finished. The final
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
- Turning the receipt off expires any receipt that was still pending: the
  bridge records `phase: expired` for it, and turning the receipt back on later
  does not send a 👍 for a message accepted long before. Receipts already sent
  or failed are left as they are.
- Behavior is the same whenever the receipt is enabled: at most one 👍 per
  accepted event, eight attempts with backoff from 5s to 300s, none for
  untrusted or rejected events.

## Follow-ups and steering

`openrig.steering_mode` controls how a follow-up behaves while the seat is
working:

- `queue` keeps all requests FIFO.
- `auto` is the default and attempts best-effort delivery only for eligible
  follow-ups in the active request's exact thread.
- `steer` attempts best-effort delivery for every short text message in the
  agent's room, whether it is a reply in the active thread, a new top-level
  message, or a reply in another thread.

In `auto`, a follow-up is eligible only when it is in the active request's exact
thread, is no longer than `openrig.steering_max_chars` (2000 by default), and
arrives while the original turn is active; top-level prompts and prompts in
another thread remain separate FIFO requests. `steer` drops the thread
condition for short text messages: everything the owner types while the agent
works reaches that turn, like typing into its terminal. This deliberately mixes
simultaneous short messages into one turn, so the agent answers them together in
the active request's thread instead of once per thread. The steered input marks
each message that came from the top level or another thread. Voice notes,
files, images, galleries, reactions and decisions keep their usual handling in
every mode, and a text above `openrig.steering_max_chars` is queued. Parts of a
related exchange (see
[Related messages](#related-messages-galleries-and-voice-notes)) are steered
whatever their length. Events received inside
`openrig.steering_batch_window_seconds` (0.5 by default) form one ordered batch.
Every source event keeps its event ID, qitem, and its own thread root; an
admitted batch receives one answer in the active request's thread carrying all
source event IDs. A batch that falls back to its own turn answers there too.

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

## The queue card

While at least one request waits behind the active one, one pinned card in the
agent's room shows what is running and what is waiting. A single active request
with nothing waiting has no queue card: the activity card, room status and
typing indicator already show it.

```text
⏳ Warteschlange: 2 Nachrichten warten · Priorität hoch.

Aktiv: Text · "Draft the release notes"
1. Datei · "Quarterly report.pdf"
2. Text · Thread · "Also check the changelog"
```

- `Aktiv` is the request the agent is working on. A request the seat has not
  started yet, for example because the seat is busy
  with other work, is shown as `Nächste Anfrage`, not as active.
- Waiting requests are numbered in the order they will run. Each entry names its
  source (`Text`, `Datei`, `Bild`, `Audio`, `Galerie`, `Entscheidung`) and
  `Thread` when it belongs to a thread.
- The card is edited in place, never re-posted, as requests arrive, start,
  finish, fail or time out. It is unpinned as soon as nothing waits any more,
  and pinned again in place when another request starts waiting. After a
  restart it is rebuilt from the saved queue, without a second card, and a card
  left pinned with nothing waiting is unpinned. Silent feedback such as a
  reaction never counts as waiting.
- A follow-up that is being steered into the active turn is counted
  in the `Aktiv (N Eingaben)` line instead of appearing as its own future turn.
  If steering falls back to a normal queued turn, it appears as a waiting entry.
- Previews are one line, cut to 96 characters. Each part that looks like a
  secret, token, credential or private path is replaced by a marker, as in the
  activity card; the rest of the preview stays. Files and images show their original filename
  or caption, galleries show the number of files and the caption; the card never
  shows local storage paths, hidden reasoning or tool output.
- Messages that only carry a reaction (silent feedback) are not listed.
- If a final answer cannot be sent after three attempts, the bridge fails that
  request's qitem and posts a notice in its thread
  (`Die Matrix-Antwort konnte nicht zugestellt werden: ...`), then refreshes the
  card and starts the next request. The failed request stays at the head of the
  queue, and later requests wait, until Matrix accepts that notice. This hold has
  no time limit yet (Issue #62).

## Related messages, galleries, and voice notes

Owners often send one thought as several Matrix events: an Element X gallery
and then the text about it, a voice note followed by a photo and a PDF, or
several parts inside one thread. The bridge delivers such an exchange to the
agent as one related context.

Which events belong together:

- A reply inside a thread belongs to that thread's exchange with no inactivity
  limit, as long as it arrives within `openrig.burst_max_span_seconds` (300 by
  default) of the exchange's first event. A later thread reply starts a new
  exchange in the same thread: its answer stays in the thread, but earlier parts
  are not restated. A rich reply belongs to the exchange of the event it
  answers, within the same span. Inside the span, relations win over timing.
- Top-level events from the same owner in the same room join while each gap
  stays within `openrig.burst_inactivity_seconds` (45 by default), the whole
  exchange stays within `openrig.burst_max_span_seconds` (300 by default), and
  the new event or the one before it carries an attachment (voice note, image,
  file, or gallery). A text sent just before or just after a gallery therefore
  joins it.
- Two plain texts never join each other; they stay separate requests as before.
  A top-level event never joins a thread by timing, and edits stay on their own.
- `openrig.burst_inactivity_seconds: 0` turns timing correlation off; thread and
  reply relations still apply.
- The bridge refuses to start if `openrig.burst_inactivity_seconds` is negative or
  `openrig.burst_max_span_seconds` is smaller than
  `openrig.burst_inactivity_seconds`, and names the setting in the error.

How the agent receives it:

- Each event is placed in its exchange when it arrives, before any download or
  transcription. A part never overtakes an earlier part of the same exchange
  that is still downloading or being transcribed, and nothing waits for a part
  that has not been sent yet.
- A part joins the exchange's request if that request has not reached the agent
  yet. If the agent is working on the exchange, the part is steered into that
  turn through the normal steering path; related context is not subject to
  `openrig.steering_max_chars`. If that turn has ended, the part arrives as a
  continuation. Steered input and continuations both restate every earlier
  part, marked `bereits zugestellt`, so the context stays complete even when
  the runtime admits steered input only as the next turn or the bridge falls
  back to a separate turn.
- A related prompt starts with the number of parts, the source event IDs in
  order and the reason they belong together, followed by one block per part:
  `Teil <n> · <Art> · <Event-ID>`. Attachments list the local path, MIME type,
  size, dimensions where known, and SHA-256 of the stored bytes; voice notes
  carry the transcript and the SHA-256 of the received audio.
- A part that could not be processed is named with its reason
  (`Nicht übernommen: …`) next to the parts that worked; the bridge never
  passes on a subset silently. The room also gets the usual failure notice.
- Every answer for the exchange is threaded under its first event (or the
  thread root) and names all source event IDs. Each event keeps its own
  OpenRig qitem.

Element X galleries (`dm.filament.gallery`, also `m.gallery`) arrive as one
event with ordered items. Each image, file, video, or audio item is decrypted,
digest-checked and stored in order; an item labelled HEIC whose bytes are JPEG
is stored as `.jpg`. A JPEG preview from the gallery is stored next to it when
present, and the prompt says when no preview was included. A gallery holds at
most 60 items.

After a restart, image and file downloads that were interrupted resume from the
saved event and write to the same stable inbox name, so no second copy appears.
Galleries resume item by item. A voice note interrupted mid-transcription keeps
its interrupted status message and is named as not processed in the related
prompt. Events sent while the bridge is stopped are not routed (see the release
notes' known limits).

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
  answer is delivered. If the seat has already taken and completed the item
  itself, the bridge treats the request as handled there: it does not send the
  prompt, posts nothing, and starts the next request.
- Additional Matrix requests remain FIFO behind the active request.
- While a request waits, a single pinned queue card shows the active request
  and ordered, sanitized previews of the waiting ones (see
  [The queue card](#the-queue-card)).

Prompt and final-answer delivery persist an operation phase before remote
effects. If a bridge exits after starting a prompt but before recording an
acknowledgement, restart fails that request explicitly rather than risking a
duplicate prompt. Final answers retry with a stable Matrix transaction ID, so a
restart can replay the send without creating a second visible event. Qitem
claim, finish, and fail operations reconcile through `rig queue show` after
restart before retrying. Attachment outbox requests use the same stable
transaction rule and are not republished after a recorded delivery.

Qitem lifecycle failures are fail-closed. If qitem creation fails, the bridge
posts an error and does not send the prompt. A rejected claim is checked with
`rig queue show` first: an item the seat completed is handled as above, and an
item that was canceled, denied or failed ends the request with one short notice
that it was not handed to the agent (queue item IDs and states stay in the
logs). If the seat completed only some items of a combined request, the request
is not split: the remaining items are failed and the same notice is posted. If the claim fails for another reason or the item cannot be read, the
request remains pending with `qitem_phase: claiming` and retries without falsely
appearing active. If finish or fail-close fails, the request remains locally pending and
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

Add `--json` to get one machine-readable result with the Matrix `event_id`,
`room_id`, filename, MIME type, size, and `sha256`. A path in an answer is not
delivery: see [Agent file delivery](AGENT_FILE_DELIVERY.md) for the contract
and a guidance block for agents that generate files.

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

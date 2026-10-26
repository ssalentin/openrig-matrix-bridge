# OpenRig Matrix Bridge 0.12.1

OpenRig Matrix Bridge 0.12.1 is a patch release for two findings from
production on 0.12.0: the bridge pinned the queue card while one request was
active and none waited
([Issue #86](https://github.com/ssalentin/openrig-matrix-bridge/issues/86)),
and `steer` mode reached only follow-ups in the active thread
([Issue #87](https://github.com/ssalentin/openrig-matrix-bridge/issues/87)).
Everything else is as in [0.12.0](RELEASE_NOTES_0.12.0.md).

## What changed

- The queue card is pinned only while at least one request waits behind the
  active one. With one active request and nothing waiting there is no card; the
  activity card, room status and typing indicator show the active turn. When a
  second request starts waiting, the card is edited or created and pinned in
  place; while several wait it keeps the active request and the ordered waiting
  previews; when the last waiting request starts or drains, it is unpinned. A
  restart unpins a stale card that 0.12.0 left behind. Silent feedback (a
  reaction) never counts as waiting and never creates a card. A request that
  waits for a busy seat keeps its "Nächste Anfrage" card, and a steered
  follow-up of the active turn does not count as waiting (#86).
- With `openrig.steering_mode: steer`, every short owner text message in an
  agent room is a steering candidate: a top-level message, a reply in the active
  thread or a reply in another thread, as if typed into the terminal. The batch
  keeps each source event ID, qitem, original thread root, order and timestamp.
  The steered text says that the later messages arrived while another request
  was active and marks each one `(new top-level message)` or
  `(sent in thread <root>)`, in arrival order. One answer is posted in the
  active request's thread and names every source event ID, both when the input is
  admitted and after the one durable fallback attempt; nothing is answered in the
  other roots. `queue` stays FIFO and `auto` keeps the same-thread rule, so the
  default (`auto`) behaves as in 0.12.0. Voice notes, files, images, galleries,
  reactions and decisions are unchanged, and a text above
  `openrig.steering_max_chars` is queued. Admission stays best effort and one
  agent turn stays active per seat (#87).

No configuration key, state file field or command is added or changed. A 0.12.0
state directory is read as it is. Cross-thread steering takes effect only when
`openrig.steering_mode` is set to `steer`; the default stays `auto`.

## Proven and NOT PROVEN

### Proven

The proof of 0.12.0 stands unchanged
([release notes](RELEASE_NOTES_0.12.0.md#proven-and-not-proven)). The
0.12.1 change is shown by unit tests and by the same Matrix integration
harness: `tests/test_queue_card_zero_waiting.py` (one active and nothing waiting,
unpin, the 0 to 1 to 0 to 1 waiting transitions edited in place, a restart with
a stale pinned card, silent feedback, a steered follow-up, and the busy-seat
control), and the `fifo-queue-notice` check, which now asserts no pinned card
while the first request runs alone, a pinned card while the second waits, and
no pinned card after the queue drains. The #87 change is shown by
`tests/test_cross_thread_steering.py` (top-level and cross-thread follow-ups
become one ordered batch injected once in `steer` mode and stay FIFO in `auto`
and `queue`; a reply in another thread keeps its own root and qitem; the
fallback, restart and completion-race cases keep the whole batch; long text,
images and the other media kinds are not pulled in) and by the
`steering-cross-thread` harness check (one steering batch with both IDs and the
origin markers, no extra turn, one answer under the active root naming every
source ID, three valid qitem histories). The retained report for the final
candidate is `final-0.12.1-integration-report.json`: the third of three
sequential locked exact-source runs at `bb58ef6`, 38 of 38 checks passed,
`evidence_valid: true`, `worktree_clean: true`. The digests of the release
wheel and sdist are those of the published `SHA256SUMS`.

### NOT PROVEN

Unchanged from 0.12.0: live Codex steering, a device SAS on a real device, the
typing indicator, queue card and galleries in Element X on a real device, a
state file from a running 0.10 bridge, and a prolonged Matrix outage. The 0.12.1
change was not checked in Element X on a real device either.

### Human-only gates

The gates listed in the [0.12.0 notes](RELEASE_NOTES_0.12.0.md#human-only-gates)
remain open. 0.12.1 adds none.

## Upgrade from 0.12.0

Use the [0.12.1 runbook](RUNBOOK_0.12.1.md): back up the configuration and the
complete state directory, install the exact 0.12.1 wheel and check the queue
card in a room (section 4 of the runbook). A pinned queue card that 0.12.0 left
behind is removed at startup.

## Roll back to 0.12.0

Restore the backup and reinstall the `v0.12.0` wheel as in the
[0.12.1 runbook](RUNBOOK_0.12.1.md#5-roll-back). Rolling back brings back the
zero-waiting pinned card of Issue #86 and the same-thread limit of `steer` mode
(Issue #87), and nothing else.

## Known limits

- In `steer` mode, simultaneous short messages from different threads are
  answered together in the active thread, which reduces the thread-local
  granularity of answers; use `auto` or `queue` where that matters.
The [0.12.0 known limits](RELEASE_NOTES_0.12.0.md#known-limits) apply
unchanged, including Issue #62 (the failure-notice hold has no time limit) and
Issue #84 (commentary redaction boundaries).

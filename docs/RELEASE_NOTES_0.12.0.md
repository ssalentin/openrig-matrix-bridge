# OpenRig Matrix Bridge 0.12.0

OpenRig Matrix Bridge 0.12.0 adds best-effort steering for eligible follow-ups
without starting a second agent or allowing concurrent Codex turns.

## What changed

- `queue`, `steer`, and default `auto` modes are supported.
- `auto` steers only short follow-ups in the exact active Matrix thread.
  Top-level prompts and prompts in another thread remain separate FIFO work.
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
- Each durably accepted human message gets exactly one 👍 (`m.annotation`),
  sent after the qitem is persisted and the request is queued or joined to a
  steering batch. It means accepted, not finished; the final answer remains the
  completion signal. Retries, restarts, duplicate polls, and batch members never
  duplicate it, and untrusted, ignored, duplicate, or rejected events get none.
  `openrig.acceptance_reaction_enabled` (default `true`) disables it.
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

- Matrix device SAS and complete cross-signing remain tracked by Issue #1.
- Activity-card duplication and legacy pending-state reconciliation remain
  separately tracked by Issues #39 and #41.
- Live Codex evidence is **not proven** for this release candidate. The
  integration harness uses a real Synapse and a deterministic OpenRig transport,
  so it does not prove delivery into a live Codex terminal. The first live
  observation is the disposable-seat check in the
  [runbook](RUNBOOK_0.12.0.md), which also holds the latency table to be filled
  with median and maximum values and the method.

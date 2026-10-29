# OpenRig Matrix Bridge 0.12.4

OpenRig Matrix Bridge 0.12.4 fixes a delivery-ledger mismatch between the
Matrix bridge and OpenRig's human-notification gateway
([Issue #101](https://github.com/ssalentin/openrig-matrix-bridge/issues/101)).
Everything else is as in
[0.12.3](RELEASE_NOTES_0.12.3.md),
[0.12.2](RELEASE_NOTES_0.12.2.md), and
[0.12.0](RELEASE_NOTES_0.12.0.md).

## What changed

- **A successful Matrix post now records delivery.** After publishing a human
  update or decision, the bridge finds the current owner-notification
  transition and writes OpenRig's episode-keyed
  `slack-owner-notification-posted` receipt with the Matrix event ID. OpenRig's
  gateway schema still names this transport kind `slack`; Matrix uses that
  existing receipt vocabulary instead of introducing a parallel transport.
- **Delivered informational updates close cleanly.** A `human-update` row is
  recorded and closed as `done/no-follow-on`. A decision remains open for the
  person's answer.
- **Retries repair the receipt without posting twice.** Decisions are
  reconciled from their persisted Matrix event IDs, and ordinary messages
  return their recorded event ID. A restart between the Matrix send and the
  OpenRig queue update therefore repairs the ledger without creating a
  duplicate message.
- **The complete transition history is read.** Receipt detection uses
  `rig queue transitions --full`, so a busy queue row cannot hide its original
  owner-notification transition behind the compact display window.

No configuration change is needed. A 0.12.3 state directory is read as it is.
The OpenRig daemon API, Matrix room format, queue ordering, and steering rules
are unchanged.

## Proven and NOT PROVEN

### Proven

The unit suite covers informational update closure, decision delivery without
closure, receipt formatting and episode keys, receipt idempotency, recovery for
an already-published decision that lacks a receipt, complete transition reads,
and message retry without a duplicate Matrix send. The committed integration
harness continues to exercise the real Matrix protocol path against a
disposable Synapse with a deterministic OpenRig transport. The retained final
candidate report records 43 of 43 checks at
`3691acb3f7d074abf7c61d8d0c6daba9f46dfe79`, including update closure, decision
delivery without closure, both owner receipts, and a one-message duplicate
check; `evidence_valid: true` and `worktree_clean: true`.

The production deployment is verified after publication with one informational
update and one decision: both must report `deliveryOutcome: posted`; the update
must close as `done/no-follow-on`; the decision must remain pending for an
answer; and a retry of the same bridge operation must not create a second
Matrix event.

### NOT PROVEN

The existing limits in the
[0.12.3 notes](RELEASE_NOTES_0.12.3.md#proven-and-not-proven) apply unchanged.
This release does not add a new OpenRig transport type or change the daemon's
Slack-named gateway schema.

### Human-only gates

The gates listed in the
[0.12.0 notes](RELEASE_NOTES_0.12.0.md#human-only-gates) remain open. This
release adds none.

## Upgrade from 0.12.3

Use the [0.12.4 runbook](RUNBOOK_0.12.4.md): back up the configuration and the
complete state directory, install the exact 0.12.4 wheel, restart one bridge
process, and run the human-update and decision smoke checks.

## Roll back to 0.12.3

Restore the backup and reinstall the `v0.12.3` wheel as described in the
[0.12.4 runbook](RUNBOOK_0.12.4.md#5-roll-back). The Matrix messages remain in
the room; 0.12.3 resumes its previous behavior and continues to report missing
owner receipts as `never-posted`.

## Known limits

The [0.12.3 known limits](RELEASE_NOTES_0.12.3.md#known-limits) apply
unchanged.

# OpenRig Matrix Bridge 0.12.1

OpenRig Matrix Bridge 0.12.1 is a patch release for
[Issue #86](https://github.com/ssalentin/openrig-matrix-bridge/issues/86), a
defect found in production on 0.12.0: the bridge pinned the queue card while
one request was active and none waited. Everything else is as in
[0.12.0](RELEASE_NOTES_0.12.0.md).

PLACEHOLDER (#86 fix PR): the paragraph under "What changed" is filled from the
pull request when it merges.

## What changed

- PLACEHOLDER (#86 fix PR): no pinned queue card while one request is active and
  none waits; the card is pinned when a request waits and unpinned when the last
  waiting request starts or drains, and a restart removes a stale pinned card.

No configuration key, state file field or command changes. A 0.12.0 state
directory is read as it is.

## Proven and NOT PROVEN

### Proven

The proof of 0.12.0 stands unchanged
([release notes](RELEASE_NOTES_0.12.0.md#proven-and-not-proven)). The
0.12.1 change is shown by unit tests and by the same Matrix integration
harness: PLACEHOLDER (#86 fix PR): name the added tests and harness check when
the fix merges. The retained report for the final candidate is
`final-0.12.1-integration-report.json`
(PLACEHOLDER: committed once from the final locked runs).

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
zero-waiting pinned card of Issue #86 and nothing else.

## Known limits

The [0.12.0 known limits](RELEASE_NOTES_0.12.0.md#known-limits) apply
unchanged, including Issue #62 (the failure-notice hold has no time limit) and
Issue #84 (commentary redaction boundaries).

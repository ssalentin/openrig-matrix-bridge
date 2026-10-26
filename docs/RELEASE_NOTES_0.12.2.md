# OpenRig Matrix Bridge 0.12.2

OpenRig Matrix Bridge 0.12.2 is a patch release for two findings from
production on 0.12.1: the bridge posted internal queue item lifecycle text into
a room while the agent was already handling the same message
([Issue #91](https://github.com/ssalentin/openrig-matrix-bridge/issues/91)), and
one installation could not show commentary unredacted
([Issue #93](https://github.com/ssalentin/openrig-matrix-bridge/issues/93)).
Everything else is as in [0.12.1](RELEASE_NOTES_0.12.1.md) and
[0.12.0](RELEASE_NOTES_0.12.0.md).

## What changed

- **A seat that completes its own queue item no longer produces room noise
  (#91).** The bridge creates a durable queue item for each owner message, and
  the agent seat can poll, claim and close it before the bridge claims it
  itself. After a rejected claim the bridge now re-reads the item. If the seat
  completed it, the request is treated as handled: no prompt is sent, no item
  is failed, nothing is posted, and the next request starts. A canceled, denied
  or failed item ends the request with one short notice that the request was
  ended in OpenRig before the agent received it; it names no queue item ID or
  state. A claim that fails for another reason, or an item that cannot be read,
  keeps the request pending and retries, with a notice that no longer uses
  internal terms. A request that combines several items of which the seat
  completed only some is not split: the remaining items are failed with the same
  short notice. A steering batch that is already in the active turn keeps its
  answer when the seat closed its items. One active turn per seat and
  exactly-once delivery are unchanged.
- **Commentary redaction can be turned off per instance (#93).**
  `openrig.commentary_redaction_enabled` defaults to `true`, which keeps all
  existing redaction. With `false`, activity-card commentary and queue-card
  previews show paths, cookies, credentials, connection URIs, fenced output and
  long secrets as the agent wrote them; whitespace normalization, the length
  bounds and Matrix HTML escaping still apply. The bridge logs one warning at
  startup while it is off. Encryption, the access token and other secret
  handling are unchanged, and a value that is not `true` or `false` is a
  configuration error. See [Security](SECURITY.md#security-and-e2ee) for the
  risk: anything the agent prints in its commentary, secrets included, is then
  visible to every room member and stays in the room history.

No other configuration key, state file field or command changes. A 0.12.1 state
directory is read as it is, and the default behavior of the new key is the
0.12.1 behavior.

## Proven and NOT PROVEN

### Proven

The proof of 0.12.0 and 0.12.1 stands unchanged
([0.12.1 notes](RELEASE_NOTES_0.12.1.md#proven-and-not-proven)). The 0.12.2
changes are shown by unit tests and by the same Matrix integration harness:
`tests/test_terminal_qitem_silence.py` (a seat that completes the item before the
bridge claim, a `done` item found by the pre-claim read after a restart,
canceled, denied and failed items without IDs or state words, an unreadable row
that stays pending and is adopted later, an admitted steering batch closed by the
seat, and a partly completed group), the `self-completed-qitems` harness check
(real Synapse; neither a top-level message nor a same-thread follow-up whose item
the seat closed is prompted or steered, no queue item ID, state or lifecycle
notice reaches the room, the next request runs, each prompted request gets one
answer), `tests/test_commentary_redaction_optout.py` (default on, every span kept
with the key off, normalization and bounds kept, queue previews, Matrix escaping,
configuration parsing and one startup warning) and the
`commentary-redaction-opt-out` harness check (the same commentary reaches the
activity card unredacted with the key off and the log has exactly one warning,
while the default instance redacts it). The legacy `legacy-pending-reconcile`
check now asserts that its notice names no queue item ID or state. The retained
report for the final candidate is `final-0.12.2-integration-report.json`
(PLACEHOLDER: committed once from the final locked runs).

### NOT PROVEN

Unchanged from 0.12.0 and 0.12.1: live Codex steering, a device SAS on a real
device, the typing indicator, queue card and galleries in Element X on a real
device, a state file from a running 0.10 bridge, and a prolonged Matrix outage.
The self-completing seat is a fake OpenRig transport. The race was seen in
production and reproduced with that fake seat; it was not run against a live
Codex seat. The opt-out was not run on the owner's production installation.

### Human-only gates

The gates listed in the [0.12.0 notes](RELEASE_NOTES_0.12.0.md#human-only-gates)
remain open. 0.12.2 adds none. Setting
`openrig.commentary_redaction_enabled: false` on an installation is the
Engineering Lead's decision, made after the 0.12.2 deploy.

## Upgrade from 0.12.1

Use the [0.12.2 runbook](RUNBOOK_0.12.2.md): back up the configuration and the
complete state directory, install the exact 0.12.2 wheel, and check that the room
shows no queue item lifecycle text. Without a configuration change the bridge
behaves as 0.12.1 except for the #91 fix. To show commentary unredacted on one
instance, set the key to `false` in its configuration after the deploy and
restart; the startup log then carries one warning.

## Roll back to 0.12.1

Restore the backup and reinstall the `v0.12.1` wheel as in the
[0.12.2 runbook](RUNBOOK_0.12.2.md#5-roll-back). 0.12.1 ignores the new key, so
it redacts commentary again, and the lifecycle text of Issue #91 comes back.

## Known limits

The [0.12.1](RELEASE_NOTES_0.12.1.md#known-limits) and
[0.12.0 known limits](RELEASE_NOTES_0.12.0.md#known-limits) apply unchanged,
including Issue #62 (the failure-notice hold has no time limit) and Issue #84
(commentary redaction boundaries). With the opt-out, none of the redaction
boundaries applies, because nothing is redacted. A request whose items the seat
completed only in part loses the remaining part: those items are failed, not
retried.

# OpenRig Matrix Bridge 0.12.3

OpenRig Matrix Bridge 0.12.3 is a patch release for two findings from
production on 0.12.2: the bridge posted a status message for every successful
media transfer
([Issue #95](https://github.com/ssalentin/openrig-matrix-bridge/issues/95)), and
messages reached the agent's pane slowly because every poll cycle and every
request read the OpenRig node inventory again, which takes seconds on a large
fleet
([Issue #96](https://github.com/ssalentin/openrig-matrix-bridge/issues/96)).
Everything else is as in [0.12.2](RELEASE_NOTES_0.12.2.md),
[0.12.1](RELEASE_NOTES_0.12.1.md) and [0.12.0](RELEASE_NOTES_0.12.0.md).

## What changed

- **Media transfer notices can be turned off (#95).**
  `media.transfer_notices_enabled` defaults to `true`, which keeps the current
  behavior. With `false` the bridge posts none of the six success messages
  (`Bild`, `Datei` and `Galerie` "… empfangen, lade und entschlüssele …" and
  "… gespeichert …"), also when a restart resumes a transfer. The media is
  still stored and sent to the agent exactly as before, with its path, size and
  SHA-256. Failures are always reported: unsupported or oversized media, media
  that is too old, a failed download or decryption, an empty gallery, and a
  gallery in which some items could not be stored (its message names them). A
  value that is not `true` or `false` is a configuration error. Voice-note
  progress messages are not covered.
- **The node inventory is read once and shared (#96).** The bridge learns which
  seats are live, busy or idle from `rig ps --nodes -A --json --full`. Concurrent
  readers now wait for one read, one snapshot serves every agent of a poll cycle
  and every request that starts within `openrig.inventory_cache_seconds` (default
  2.0), and an older snapshot, up to `openrig.inventory_max_age_seconds`
  (default 10.0), is used at once while one background read refreshes it; past
  that bound the bridge waits for a fresh read. A failed read is never reused and
  never blocks later reads. The start path no longer reads the inventory on the
  event loop. Two safeguards keep a slightly old snapshot from changing
  delivery: an inventory read before the active prompt went in no longer
  cancels a steering attempt (the transcript turn identity decides), and one read
  before the bridge saw the agent's last turn finish no longer holds the next
  prompt. Each request records how long the inventory, qitem create, qitem claim
  and pane send took (`spans` in the `latency_samples` of `state.json`). Setting
  both values to 0 turns the cache off: a caller that finds no read in progress
  starts one, and callers that arrive during a read still share it.
  `openrig.inventory_cache_seconds` must be between 0 and 10 and
  `openrig.inventory_max_age_seconds` at least that value and at most 60.

Queue ordering, exactly-once delivery, source-event attribution, the qitem
lifecycle and the steering fallback rules are unchanged. A 0.12.2 state
directory is read as it is. The inventory cache is on by default, with the defaults above; the media notices
stay on by default.

## Proven and NOT PROVEN

### Proven

The proof of 0.12.0, 0.12.1 and 0.12.2 stands unchanged
([0.12.2 notes](RELEASE_NOTES_0.12.2.md#proven-and-not-proven)). The 0.12.3
changes are shown by unit tests and by the same Matrix integration harness:
`tests/test_media_transfer_notices.py` (the default and `true` keep every image
and file notice, `false` posts nothing while the stored bytes and the agent
prompt are unchanged, the resume paths stay quiet, failures are still reported,
configuration parsing), the `media-transfer-notices-off` harness check (an
encrypted image, a PDF and a four-item gallery are stored byte-for-byte and
forwarded, and no received, progress or stored notice reaches the room),
`tests/test_inventory_cache.py` (cache hit, expiry, single-flight, a stale
snapshot served while one read refreshes it, the hard bound, failures never
cached and never wedging later reads, one inventory per poll cycle, the start
path off the event loop, the two stale-read rules each with a control, latency
spans, configuration bounds) and the `inventory-latency` harness check (with a
fake `rig ps` delayed by 3.4 s: 20 idle prompts, 19 prompts right after a
finished turn and 20 steered follow-ups, each delivered exactly once, within the
latency targets of the issue). The retained report for the final candidate is
`final-0.12.3-integration-report.json`: the third of three sequential locked
exact-source runs at `48c3540`, 42 of 42 checks passed, `evidence_valid: true`,
`worktree_clean: true`. The digests of the release wheel and sdist are those of
the published `SHA256SUMS`.

### NOT PROVEN

Unchanged from 0.12.0 to 0.12.2: live Codex steering, a device SAS on a real
device, the typing indicator, queue card and galleries in Element X on a real
device, a state file from a running 0.10 bridge, and a prolonged Matrix outage.
In addition, the latency figures of the harness come from a deterministic fake
transport with a 3.4 s fake inventory; they are not measurements on the
production fleet. The issue's targets on the production-class host (idle
intake-to-injection median 1.5 s and p95 3 s, steering median 2 s and p95 4 s
over at least 20 runs each) are measured by the Engineering Lead after the
deploy. The media switch was not run on the owner's production installation.

### Human-only gates

The gates listed in the [0.12.0 notes](RELEASE_NOTES_0.12.0.md#human-only-gates)
remain open. 0.12.3 adds none. Setting `media.transfer_notices_enabled: false`
on an installation, and the production latency measurement, are the Engineering
Lead's decisions after the 0.12.3 deploy.

## Upgrade from 0.12.2

Use the [0.12.3 runbook](RUNBOOK_0.12.3.md): back up the configuration and the
complete state directory, install the exact 0.12.3 wheel, and check delivery and
the media behavior of the instance. No configuration change is needed. The
inventory cache takes effect with its defaults; to silence media notices on one
instance, set `media.transfer_notices_enabled: false` after the deploy and
restart.

## Roll back to 0.12.2

Restore the backup and reinstall the `v0.12.2` wheel as in the
[0.12.3 runbook](RUNBOOK_0.12.3.md#5-roll-back). 0.12.2 ignores the three new
keys, so the media notices return and the inventory is read as before.

## Known limits

The [0.12.2](RELEASE_NOTES_0.12.2.md#known-limits),
[0.12.1](RELEASE_NOTES_0.12.1.md#known-limits) and
[0.12.0 known limits](RELEASE_NOTES_0.12.0.md#known-limits) apply unchanged,
including Issue #62 and Issue #84.

- The busy/idle state the bridge acts on can be up to
  `openrig.inventory_max_age_seconds` old, plus the duration of the poll cycle
  that shares one inventory across all agents. Input that reaches a seat that has
  just become busy is queued by Codex and adopted by the existing admission
  logic. Set both values to 0 to read the inventory for every new caller, at the
  latency cost described in Issue #96.
- The bridge still starts a `rig` process for every inventory read; a long-lived
  OpenRig daemon API or stream is not part of this release.
- With `media.transfer_notices_enabled: false`, nothing in the room says that a
  media transfer began or finished; the upload shown by Matrix and the agent's
  answer are the only signs. Voice-note progress messages are unchanged.

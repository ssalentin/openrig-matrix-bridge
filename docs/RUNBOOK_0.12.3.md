# 0.12.3 release and rollback runbook

Operator steps for installing and verifying the `0.12.3` candidate on a host
that runs 0.12.2. The Engineering Lead owns merge, tag publication, production
deployment, and cleanup. Only the delta to the
[0.12.0 runbook](RUNBOOK_0.12.0.md) is repeated here; its backup, install,
configuration and acceptance steps apply as written, with `0.12.3` in place of
`0.12.0` in every file name and version check.

## Release checklist

- [ ] QA and review approve one immutable candidate SHA, and
  `docs/artifacts/final-0.12.3-integration-report.json` names that SHA with
  `worktree_clean: true` and `evidence_valid: true`.
- [ ] The pull requests of Issues #95 and #96 (#97 and #99) are merged into
  `main`, followed by the release pull request (version 0.12.3, changelog,
  notes, this runbook).
- [ ] CI on the merge commit in `main` is green.
- [ ] Create the annotated tag `v0.12.3` on that commit.
- [ ] Publish the GitHub Release with the wheel, the sdist, the release notes,
  and `SHA256SUMS`.
- [ ] Record the observed value of each item in the acceptance record below.

## 1. Record the approved candidate

```bash
sha256sum -c SHA256SUMS
sha256sum openrig_matrix_bridge-0.12.3-py3-none-any.whl
/opt/openrig-matrix/venv/bin/openrig-matrix-bridge --version
```

Expected console version after the install: `0.12.3`.

## 2. Back up and install

Follow sections 2 and 3 of the [0.12.0 runbook](RUNBOOK_0.12.0.md#2-back-up-state-before-upgrading)
with the 0.12.3 wheel, and retain the known-good `v0.12.2` wheel until the
rollout is accepted.

## 3. Check configuration and start one process

Section 4 of the 0.12.0 runbook applies unchanged: `doctor`, one bridge process,
room status `bereit`.

The three new keys are optional. `openrig.inventory_cache_seconds` (default 2.0,
0 to 10) and `openrig.inventory_max_age_seconds` (default 10.0, at least the
first and at most 60) tune the shared node inventory; leave them at the defaults
unless the Engineering Lead decides otherwise. `media.transfer_notices_enabled`
defaults to `true`. Where the owner wants no per-transfer media messages, set it
to `false` only after the 0.12.3 install is accepted and restart the service.
A value outside those bounds, or a media switch that is not `true` or `false`,
stops the start with a configuration error.

## 4. Production smoke check

Send one ordinary prompt to an idle seat and then two rapid same-thread
follow-ups while it works. Each source event reaches the agent once, and the
answer arrives under the original thread root. Read the `latency_samples` in
`state.json` (`spans` carries `inventory_ms`, `qitem_create_ms`, `qitem_claim_ms`
and `pane_send_ms`): the intake-to-injection times should now be well below the
seconds a `rig ps` read takes. The Engineering Lead records the idle and steering
medians and p95 over at least 20 runs on this host; the harness figures are not
a substitute.

Send one image, one file and a small gallery. Where `media.transfer_notices_enabled`
is `true`, each shows its received and stored messages. Where it is `false`, no
received, progress or stored message appears, the agent still receives each item
with its path and SHA-256, and a deliberately unsupported or oversized file is
still reported.

A duplicate or missing delivery, a steered follow-up that falls back to its own
turn only because of an old inventory, or a success notice on an instance with the
media switch `false` is a rollout failure.

## 5. Roll back

```bash
sudo systemctl stop openrig-matrix-bridge.service
sudo mv /var/lib/openrig-matrix-bridge /var/lib/openrig-matrix-bridge.0.12.3-failed
sudo cp -a "$backup/state" /var/lib/openrig-matrix-bridge
sudo cp -a "$backup/etc/." /etc/openrig-matrix-bridge/
/opt/openrig-matrix/venv/bin/python -m pip install --force-reinstall \
  /path/to/openrig_matrix_bridge-0.12.2-py3-none-any.whl
sudo systemctl start openrig-matrix-bridge.service
```

Verify the restored version, doctor result, one-process invariant, room status,
and one encrypted Matrix round trip. 0.12.2 ignores the three new keys: media
notices return and the inventory is read as before. Preserve failed state and
logs for diagnosis.

## Acceptance record

| Item | Observed value |
|---|---|
| Candidate commit SHA | |
| Wheel SHA-256 | |
| Installed version | |
| Doctor result | |
| One-process check | |
| Idle and steering intake-to-injection median and p95 (at least 20 runs each) | |
| Media switch of this instance and what a test image, file and gallery showed | |
| Backup path | |
| Rollback decision | |

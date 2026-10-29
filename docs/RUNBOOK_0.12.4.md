# 0.12.4 release and rollback runbook

Operator steps for installing and verifying the `0.12.4` candidate on a host
that runs 0.12.3. Only the delta to the
[0.12.0 runbook](RUNBOOK_0.12.0.md) is repeated here; its backup, install,
configuration, and rollback steps apply as written, with `0.12.4` in place of
`0.12.0` in every file name and version check.

## Release checklist

- [ ] Issue #101 is merged into `main`, followed by the release pull request
  containing version 0.12.4, the changelog, these notes, and this runbook.
- [ ] CI on the release candidate is green.
- [ ] Create the annotated tag `v0.12.4` on that commit.
- [ ] Publish the GitHub Release with the wheel, sdist, release notes, and
  `SHA256SUMS`.
- [ ] Record each item in the acceptance record below.

## 1. Record the approved candidate

```bash
sha256sum -c SHA256SUMS
sha256sum openrig_matrix_bridge-0.12.4-py3-none-any.whl
/opt/openrig-matrix/venv/bin/openrig-matrix-bridge --version
```

Expected console version after installation: `0.12.4`.

## 2. Back up and install

Follow sections 2 and 3 of the
[0.12.0 runbook](RUNBOOK_0.12.0.md#2-back-up-state-before-upgrading) with the
0.12.4 wheel. Retain the known-good `v0.12.3` wheel and the complete bridge
configuration and state backup until rollout is accepted.

## 3. Check configuration and start one process

Section 4 of the 0.12.0 runbook applies unchanged. `doctor` must report ready,
the service must have exactly one bridge process, and every configured room
must report `bereit`.

No configuration change is required for issue #101.

## 4. Production delivery smoke check

Create one informational row for `sebastian@external` with
`--human-intent update` and one decision row with `--human-intent decision`.
Wait for the bridge's human-queue poll to publish both.

The update must:

- appear once in Matrix;
- have one `slack-owner-notification-posted` receipt whose
  `notification_key` is `<qitemId>:<transitionId>` and whose `message_ts` is
  the Matrix event ID; and
- close as `done/no-follow-on`.

The decision must:

- appear once in Matrix;
- have the same kind of owner-delivery receipt; and
- remain pending until its answer is submitted.

A retry or bridge restart between publishing and recording the receipt must
reconcile the persisted Matrix event ID without posting a duplicate.

## 5. Roll back

```bash
sudo systemctl stop openrig-matrix-bridge.service
sudo mv /var/lib/openrig-matrix-bridge /var/lib/openrig-matrix-bridge.0.12.4-failed
sudo cp -a "$backup/state" /var/lib/openrig-matrix-bridge
sudo cp -a "$backup/etc/." /etc/openrig-matrix-bridge/
/opt/openrig-matrix/venv/bin/python -m pip install --force-reinstall \
  /path/to/openrig_matrix_bridge-0.12.3-py3-none-any.whl
sudo systemctl start openrig-matrix-bridge.service
```

Verify the restored version, doctor result, one-process invariant, room status,
and one encrypted Matrix round trip. Preserve failed state and logs for
diagnosis.

## Acceptance record

| Item | Observed value |
|---|---|
| Candidate commit SHA | |
| Wheel SHA-256 | |
| Installed version | |
| Doctor result | |
| One-process check | |
| Informational update receipt and final state | |
| Decision receipt and pending state | |
| Duplicate-send check | |
| Backup path | |
| Rollback decision | |

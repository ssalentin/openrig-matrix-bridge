# 0.12.1 release and rollback runbook

Operator steps for installing and verifying the `0.12.1` candidate on a host
that runs 0.12.0. The Engineering Lead owns merge, tag publication, production
deployment, and cleanup. Only the delta to the
[0.12.0 runbook](RUNBOOK_0.12.0.md) is repeated here; its backup, install,
configuration and acceptance steps apply as written, with `0.12.1` in place of
`0.12.0` in every file name and version check.

## Release checklist

- [ ] QA and review approve one immutable candidate SHA, and
  `docs/artifacts/final-0.12.1-integration-report.json` names that SHA with
  `worktree_clean: true` and `evidence_valid: true`.
- [ ] The pull request of Issue #86 is merged into `main`, followed by the
  release pull request (version 0.12.1, changelog, notes, this runbook).
- [ ] CI on the merge commit in `main` is green.
- [ ] Create the annotated tag `0.12.1` on that commit.
- [ ] Publish the GitHub Release with the wheel, the sdist, the release notes,
  and `SHA256SUMS`.
- [ ] Record the observed value of each item in the acceptance record below.

## 1. Record the approved candidate

```bash
sha256sum -c SHA256SUMS
sha256sum openrig_matrix_bridge-0.12.1-py3-none-any.whl
/opt/openrig-matrix/venv/bin/openrig-matrix-bridge --version
```

Expected console version after the install: `0.12.1`.

## 2. Back up and install

Follow sections 2 and 3 of the [0.12.0 runbook](RUNBOOK_0.12.0.md#2-back-up-state-before-upgrading)
with the 0.12.1 wheel, and retain the known-good `v0.12.0` wheel until the
rollout is accepted.

## 3. Check configuration and start one process

Section 4 of the 0.12.0 runbook applies unchanged: `doctor`, one bridge process,
room status `bereit`.

## 4. Production smoke check

Send one ordinary prompt to an idle seat. While it is active, the room has the
activity card, the `arbeitet` status and typing, and **no pinned queue card**.
Send a second prompt while the first is active: one queue card appears and is
pinned, listing the active request and one waiting request. When the second
request starts, the pinned card disappears. After the last answer the status
returns to `bereit`.

A pinned card with "0 Nachrichten wartet" during a single active request is a
rollout failure. Restart the bridge while a request is active and none waits:
a card pinned by 0.12.0 must be gone after the start.

## 5. Roll back

```bash
sudo systemctl stop openrig-matrix-bridge.service
sudo mv /var/lib/openrig-matrix-bridge /var/lib/openrig-matrix-bridge.0.12.1-failed
sudo cp -a "$backup/state" /var/lib/openrig-matrix-bridge
sudo cp -a "$backup/etc/." /etc/openrig-matrix-bridge/
/opt/openrig-matrix/venv/bin/python -m pip install --force-reinstall \
  /path/to/openrig_matrix_bridge-0.12.0-py3-none-any.whl
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
| Queue card check (no card at one active, card when one waits, unpinned after) | |
| Backup path | |
| Rollback decision | |

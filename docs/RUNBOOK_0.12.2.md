# 0.12.2 release and rollback runbook

Operator steps for installing and verifying the `0.12.2` candidate on a host
that runs 0.12.1. The Engineering Lead owns merge, tag publication, production
deployment, and cleanup. Only the delta to the
[0.12.0 runbook](RUNBOOK_0.12.0.md) is repeated here; its backup, install,
configuration and acceptance steps apply as written, with `0.12.2` in place of
`0.12.0` in every file name and version check.

## Release checklist

- [ ] QA and review approve one immutable candidate SHA, and
  `docs/artifacts/final-0.12.2-integration-report.json` names that SHA with
  `worktree_clean: true` and `evidence_valid: true`.
- [ ] The pull request of Issues #91 and #93 (#92) is merged into `main`,
  followed by the release pull request (version 0.12.2, changelog, notes, this
  runbook).
- [ ] CI on the merge commit in `main` is green.
- [ ] Create the annotated tag `0.12.2` on that commit.
- [ ] Publish the GitHub Release with the wheel, the sdist, the release notes,
  and `SHA256SUMS`.
- [ ] Record the observed value of each item in the acceptance record below.

## 1. Record the approved candidate

```bash
sha256sum -c SHA256SUMS
sha256sum openrig_matrix_bridge-0.12.2-py3-none-any.whl
/opt/openrig-matrix/venv/bin/openrig-matrix-bridge --version
```

Expected console version after the install: `0.12.2`.

## 2. Back up and install

Follow sections 2 and 3 of the [0.12.0 runbook](RUNBOOK_0.12.0.md#2-back-up-state-before-upgrading)
with the 0.12.2 wheel, and retain the known-good `v0.12.1` wheel until the
rollout is accepted.

## 3. Check configuration and start one process

Section 4 of the 0.12.0 runbook applies unchanged: `doctor`, one bridge process,
room status `bereit`.

`openrig.commentary_redaction_enabled` is absent or `true` on every host unless
the owner decided otherwise. On the one instance where the owner wants
commentary unredacted, set it to `false` only after the 0.12.2 install is
accepted, restart the service, and confirm that the journal carries exactly one
line containing `openrig.commentary_redaction_enabled is false`. A value other
than `true` or `false` stops the start with a configuration error.

## 4. Production smoke check

Send one ordinary prompt to an idle seat. The room shows the activity card, the
`arbeitet` status and typing, and no text that names a queue item, a state such
as `done` or `terminal`, or says that a queue item could not be claimed. If the
agent seat completes the prompt's queue item by itself, the room still shows no
such text and the request does not run twice.

Where the instance keeps the default, ask the agent to mention a path such as
`/root/.openrig/...` in its progress text: the activity card shows
`[lokaler Pfad]`. Where the key is `false`, the same text shows the path as
written, and the startup warning is in the journal exactly once per start.

A queue item lifecycle message, an unredacted path on an instance that keeps the
default, or a missing or repeated startup warning on an instance with the key
`false` is a rollout failure.

## 5. Roll back

```bash
sudo systemctl stop openrig-matrix-bridge.service
sudo mv /var/lib/openrig-matrix-bridge /var/lib/openrig-matrix-bridge.0.12.2-failed
sudo cp -a "$backup/state" /var/lib/openrig-matrix-bridge
sudo cp -a "$backup/etc/." /etc/openrig-matrix-bridge/
/opt/openrig-matrix/venv/bin/python -m pip install --force-reinstall \
  /path/to/openrig_matrix_bridge-0.12.1-py3-none-any.whl
sudo systemctl start openrig-matrix-bridge.service
```

Verify the restored version, doctor result, one-process invariant, room status,
and one encrypted Matrix round trip. 0.12.1 ignores
`openrig.commentary_redaction_enabled` and redacts again. Preserve failed state
and logs for diagnosis.

## Acceptance record

| Item | Observed value |
|---|---|
| Candidate commit SHA | |
| Wheel SHA-256 | |
| Installed version | |
| Doctor result | |
| One-process check | |
| No queue item lifecycle text in the room | |
| Redaction setting of this instance and its startup warning count | |
| Backup path | |
| Rollback decision | |

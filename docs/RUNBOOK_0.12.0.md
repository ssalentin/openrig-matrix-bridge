# 0.12.0 release and rollback runbook

Operator steps for installing and verifying the `0.12.0` candidate on an
existing host. The Engineering Lead owns merge, tag publication, production
deployment, and cleanup.

## Release checklist

The Engineering Lead walks this list in order. Record the observed value for
each item in the acceptance record at the end of this runbook.

- [ ] QA and review approve one immutable candidate SHA, and
  `docs/artifacts/final-0.12.0-integration-report.json` names that SHA with
  `worktree_clean: true` and `evidence_valid: true`.
- [ ] Merge pull request #34 into `main` (milestone 0.12.0, tracker #46; it
  closes #4 and #36).
- [ ] CI on the merge commit in `main` is green.
- [ ] Create the annotated tag `0.12.0` on that commit.
- [ ] Publish the GitHub Release with the wheel, the sdist, the release notes,
  and `SHA256SUMS`.
- [ ] Deploy that published wheel to production with a backup and the 0.11.0
  rollback artifact (sections 1 to 4).
- [ ] `doctor` passes and exactly one bridge process runs (section 4).
- [ ] Observe production using the smoke check (section 6), including the
  thumbs-up receipt if you enabled it, and decide to accept or roll back (section 7).

### Evidence boundary

Live Codex evidence is **not proven** for this candidate. The development cell
did not run live Codex turns, so the issue #46 criterion for a live two-follow-up
run is open. The available evidence is deterministic unit and integration
coverage plus real-Matrix-protocol runs against a disposable Synapse with a fake
OpenRig transport. Section 5 is the first live-Codex observation and must happen
on a disposable seat before production traffic.

### Latency measurements

Not yet measured on a live seat: live Codex turns were not run in the 0.12.0
gate. QA or the builder fills the live table after running section 5, with the
method and boundary stated. The harness `steering_latency` median and maximum for
the deterministic transport are in the committed
`docs/artifacts/final-0.12.0-integration-report.json`; they are not live-Codex
latency and must not be copied into the live table.

| Boundary (live seat) | Runs | Median | Max | Method |
|---|---|---|---|---|
| Bridge intake to pane injection | 0 | not measured | not measured | live Codex not run in the 0.12.0 gate |
| Bridge intake to transcript admission | 0 | not measured | not measured | live Codex not run in the 0.12.0 gate |
| Bridge intake to 👍 acceptance reaction | 0 | not measured | not measured | live Codex not run in the 0.12.0 gate |

Deterministic harness latency, from the `steering_latency` section of
`docs/artifacts/final-0.12.0-integration-report.json`: fake OpenRig transport and
a loopback Synapse, not a live seat. Admission includes up to one bridge poll
interval. Maxima include the crash, restart and delayed-probe scenarios. These
latencies depend on host load: they are higher than in the earlier `0f533e7`
report (prompt injection and admission medians 124.4 and 315.4 ms), which ran on
a less busy host. They are not a performance benchmark.

| Boundary (harness) | Median | Max |
|---|---|---|
| Prompt: intake to injection | 266.5 ms | 5842.6 ms |
| Prompt: intake to admission | 643.6 ms | 6060.7 ms |
| Prompt: intake to 👍 | 433.8 ms | 1605.8 ms |
| Steer: intake to admission | 1351.9 ms | 1873.6 ms |

## 1. Record the approved candidate

Use the exact commit SHA, wheel, and `SHA256SUMS` named in the QA/review handoff.
Do not install a rebuild with the same version if its digest differs.

```bash
sha256sum -c SHA256SUMS
sha256sum openrig_matrix_bridge-0.12.0-py3-none-any.whl
python -m pip show openrig-matrix-bridge
/opt/openrig-matrix/venv/bin/openrig-matrix-bridge --version
```

Expected console version: `0.12.0`.

## 2. Back up state before upgrading

Stop the service and confirm that only one bridge process exists:

```bash
sudo systemctl stop openrig-matrix-bridge.service
pgrep -af openrig_matrix_bridge || true
```

`deploy/openrig-matrix-start` execs the bridge, so systemd's default SIGTERM
reaches it directly. The bridge handles SIGTERM by running its existing
shutdown cleanup (stop the pollers, clear Matrix typing, close the client) and
exiting 0. If cleanup cannot finish, for example because the homeserver is
unreachable, systemd sends SIGKILL after `TimeoutStopSec=20`, as before. SIGINT
behavior is unchanged. Without the SIGTERM handling, a stop or restart left
"OpenRig schreibt …" visible for up to 30 seconds; a crash or SIGKILL still
does, because the homeserver's typing timeout is 30 seconds.

Back up the service definition, configuration, and complete state directory:

```bash
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/var/backups/openrig-matrix-bridge-${timestamp}"
sudo install -d -m700 "$backup"
sudo cp -a /etc/openrig-matrix-bridge "$backup/etc"
sudo cp -a /var/lib/openrig-matrix-bridge "$backup/state"
sudo cp -a /etc/systemd/system/openrig-matrix-bridge.service "$backup/service"
```

Retain the known-good 0.11.0 wheel and the backup until rollout is accepted.
Do not delete or recreate the Matrix crypto store as part of the upgrade.

## 3. Install the exact wheel

```bash
/opt/openrig-matrix/venv/bin/python -m pip install --force-reinstall \
  /path/to/openrig_matrix_bridge-0.12.0-py3-none-any.whl
/opt/openrig-matrix/venv/bin/openrig-matrix-bridge --version
```

The installed version alone does not prove the commit. Keep the approved wheel
digest and provenance record beside the backup.

## 4. Check configuration and start one process

Ensure the token is supplied by the existing secret mechanism without printing
it:

```bash
MATRIX_ACCESS_TOKEN="$(systemctl show-environment | sed -n 's/^MATRIX_ACCESS_TOKEN=//p')" \
  /opt/openrig-matrix/venv/bin/openrig-matrix-bridge doctor \
  --config /etc/openrig-matrix-bridge/config.yaml
```

If `systemctl` does not provide the token, run the same command in the
service's configured secret environment. Do not paste the token onto a command
line that is captured in shell history.

Start and inspect:

```bash
sudo systemctl daemon-reload
sudo systemctl start openrig-matrix-bridge.service
sudo systemctl status openrig-matrix-bridge.service --no-pager
pgrep -af openrig_matrix_bridge
journalctl -u openrig-matrix-bridge.service --since '10 minutes ago'
```

Expected: exactly one bridge process, no authentication/configuration crash, and
the Matrix rooms return to `bereit`.

## 5. Verify steering on a disposable seat first

Before production ownership traffic, use a dedicated disposable Codex seat:

1. Start a deliberately long tool turn.
2. Send two quick Matrix replies in that turn's existing thread.
3. Confirm both source event IDs appear in the same injected batch.
4. Confirm each transcript user message appears once and the batch receives one
   Matrix answer.
5. Repeat after a bridge restart and retain redacted timing evidence. Send the
   follow-ups only after the restarted bridge has finished its initial sync:
   events delivered during that sync are not routed. This predates 0.12.0. Such
   a message is not delivered and, with the receipt enabled, gets no 👍, which
   makes the gap visible to the owner.

Do not use the OpenRig production bridge or this project-cell seat as the
disposable target.

## 6. Production smoke check

After release approval, send one ordinary Matrix prompt and then two rapid
same-thread follow-ups while the seat is working. Record:

- Matrix intake time for each event;
- pane-injection time when available;
- transcript admission time and turn ID;
- number of transcript user records and Matrix answers;
- qitem closure for every source event;
- with `openrig.acceptance_reaction_enabled: true`, exactly one 👍 on each of
  your own messages once it is accepted, and none on a message from an
  untrusted sender (the 👍 means accepted, not finished); with the default
  (off), no 👍 at all; and
- any remaining inability to observe a pane boundary.

A duplicate transcript input, duplicate side effect, missing qitem, multiple
answers for one batch, or wrong thread root is a rollout failure.

## 7. Roll back

```bash
sudo systemctl stop openrig-matrix-bridge.service
sudo mv /var/lib/openrig-matrix-bridge /var/lib/openrig-matrix-bridge.0.12.0-failed
sudo cp -a "$backup/state" /var/lib/openrig-matrix-bridge
sudo cp -a "$backup/etc/." /etc/openrig-matrix-bridge/
/opt/openrig-matrix/venv/bin/python -m pip install --force-reinstall \
  /path/to/openrig_matrix_bridge-0.11.0-py3-none-any.whl
sudo systemctl start openrig-matrix-bridge.service
```

Verify the restored version, doctor result, one-process invariant, room status,
and one encrypted Matrix round trip. Preserve failed state and logs for
diagnosis.

## 8. Acceptance record

| Item | Observed value |
|---|---|
| Candidate commit SHA | |
| Wheel SHA-256 | |
| Installed version | |
| Doctor result | |
| One-process check | |
| Disposable live-Codex steering result | |
| Production smoke result | |
| Backup path | |
| Rollback decision | |

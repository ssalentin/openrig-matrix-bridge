# Redacted artifacts

This directory contains evidence from executable checks and manual product
walkthroughs. An artifact is useful only when it records the command or user
path, the observed result, the tested commit, and the environment boundary.

## Rules

- Do not commit access tokens, passwords, recovery keys, device private keys,
  raw Matrix crypto state, `state.json`, or unredacted homeserver logs.
- Replace real Matrix user IDs, room IDs, homeserver URLs, and private hostnames
  with synthetic values such as `@owner:matrix.example.org` and
  `https://matrix.example.org`.
- Replace private rig, seat, room, and file names when they identify a person or
  private infrastructure.
- Prefer narrow excerpts over complete environment dumps.
- Inspect screenshots visually. Text search cannot find every visible room name
  or notification preview.
- Keep the raw capture outside the repository until it has been reviewed.

## Required artifacts

| Artifact | What it should prove | Provenance |
|---|---|---|
| `setup-doctor.txt` | The setup and doctor path on disposable Synapse, including failure handling | `88dccb3c92329918c62b68f871022d0972377cd8`; report `evidence_valid: true` |
| `room-status.txt` | Real encrypted room, invite/join, topic status, structured status, and avatar | `88dccb3c92329918c62b68f871022d0972377cd8`; report `evidence_valid: true` |
| `ordinary-chat.txt` | Encrypted text, typing, qitem history, thread relation, commentary, answer, and FIFO completion gating | `88dccb3c92329918c62b68f871022d0972377cd8`; report `evidence_valid: true` |
| `test-path.txt` | Unit, lint, Markdown, link, release, and Matrix integration commands with explicit boundaries | `88dccb3c92329918c62b68f871022d0972377cd8` |
| `element-web-verification.txt` | Real Element Web client behavior and the current verification-unavailable boundary | `issue-1-verification-state` manual investigation; no success claim |
| `issue-3-integration-report.json` | Machine-readable report for all 17 checks, including audio routing, admitted attachment snapshots, restart recovery, and canonical text/voice/file threads | Source `fe6f1271758773dc90ac73975f03763515db7732`; `worktree_clean: true` |
| `integration-report.json` | Machine-readable report for all 14 checks with clean source provenance | Source `539f8b9ef5c6651f4c0a65cbb6f2f9a083ef58cb`; `worktree_clean: true` |
| `issue-11-integration-report.json` | Machine-readable report for all 19 checks, including tracked legacy fail-closed and process exits after qitem claim/final answer | Source `5b70d345b6598f2ca50faf86216f6ba353166ead`; `worktree_clean: true` |
| `issue-12-integration-report.json` | Machine-readable report for 19 checks after fail-closed qitem lifecycle changes | Source `2b50efd164591880bf3c2443c08fdb0fb0dff60e`; `worktree_clean: true` |
| `matrix-stability-report.json` | Last of three consecutive exact 20-check runs after Matrix observation and restart-barrier hardening | Source `b1e8d17a1d758da3655deb6eb4aba237d2b0ebbc`; `worktree_clean: true` |
| `issue-4-integration-report.json` | Machine-readable report for 22 checks, including queue/steer/auto modes, ordered same-thread batches, asynchronous admission grace, fallback, restart recovery, completion-race adoption, all-terminal qitem recovery, legacy-state identity safety, and unrelated-turn attribution | Source `774d1038b61f79fbf28c6709a8d7c5dfbc8dee99`; `worktree_clean: true`; last of three consecutive passing runs |
| `final-0.11.0-integration-report.json` | Final 0.11.0 candidate Matrix run: 20/20 checks, `evidence_valid: true` | Source `3b21b72330a9b0810457a3ee4d11fdd2c144ae90`; `worktree_clean: true` |
| `final-0.12.3-integration-report.json` | PLACEHOLDER: the 0.12.3 candidate Matrix run, committed once from QA's final locked run 3 | PLACEHOLDER: candidate commit |
| `final-0.12.2-integration-report.json` | Final 0.12.2 candidate Matrix run: 40/40 checks, the 0.12.1 set plus `self-completed-qitems` (issue #91) and `commentary-redaction-opt-out` (issue #93), with `steering_latency` for the deterministic transport (not live Codex). Third of three sequential locked runs; `evidence_valid: true` | `37aad687dd8dd893d97579611c77a56552a49ac6`; `worktree_clean: true`; the release wheel and sdist digests are those of the published `SHA256SUMS` |
| `final-0.12.1-integration-report.json` | Final 0.12.1 candidate Matrix run: 38/38 checks, the 0.12.0 set plus `steering-cross-thread` (issue #87) and the `fifo-queue-notice` assertions for issue #86, with `steering_latency` for the deterministic transport (not live Codex). Third of three sequential locked runs; `evidence_valid: true` | `bb58ef6117238ec7ada39536940d53befc269331`; `worktree_clean: true`; the release wheel and sdist digests are those of the published `SHA256SUMS` |
| `final-0.12.0-integration-report.json` | Final 0.12.0 candidate Matrix run: 37/37 checks, including steering, the optional acceptance 👍 receipt and its default-off check, typing and status continuity, related messages and galleries, the queue card, agent file delivery, finish and 0.10 pending reconciliation, the single activity card after a lost send, and sensitive-span redaction in activity commentary, with `steering_latency` for the deterministic transport (not live Codex). Third of three sequential locked runs; `evidence_valid: true` | `d999efb8b7eb847306f7eabcac66efc6df9e795e`; `worktree_clean: true`; sdist `7829cda8caa1d3b17e28b5867796af0f2561ba70adcdc6c5633c520fdf629077`, wheel `0681f7d64bd658b3a16b5212e4abd62d9853641573af737fa433af0dea2b6593` (SHA-256), `tools/verify_release.py` and a clean-venv wheel install passed |

Text artifacts are acceptable when they capture real command output. A
screenshot alone is insufficient if the room context is ambiguous.

The public Element Web images and their crop/redaction provenance are documented
separately in [`docs/screenshots/README.md`](../screenshots/README.md). They
illustrate synthetic harness rendering and are not live-Codex or device-trust
evidence.

## Manifest

| File | Captured at | Tested source commit | Environment | Command/path | Redaction review |
|---|---|---|---|---|---|
| `integration-report.json` | `2026-10-24T18:47:39Z` | `539f8b9ef5c6651f4c0a65cbb6f2f9a083ef58cb` | Disposable Synapse on loopback; fake OpenRig CLI | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit 539f8b9ef5c6651f4c0a65cbb6f2f9a083ef58cb` | Temporary config path removed; token/room/private-infrastructure scan clean |
| `setup-doctor.txt` | `2026-10-24T17:22:44Z` | `88dccb3c92329918c62b68f871022d0972377cd8` | Disposable Synapse; fake OpenRig CLI | Same harness run as `integration-report.json` | Derived from redacted report; no credentials |
| `room-status.txt` | `2026-10-24T17:22:44Z` | `88dccb3c92329918c62b68f871022d0972377cd8` | Disposable Synapse; fake OpenRig CLI | Same harness run as `integration-report.json` | Synthetic localhost identities only |
| `ordinary-chat.txt` | `2026-10-24T17:22:44Z` | `88dccb3c92329918c62b68f871022d0972377cd8` | Disposable Synapse; fake OpenRig CLI | Same harness run as `integration-report.json` | No IDs or tokens; boundary stated |
| `test-path.txt` | `2026-10-24T17:22:44Z` | `88dccb3c92329918c62b68f871022d0972377cd8` | Repository docs/tests plus disposable Synapse | Commands listed inside artifact | No credentials or private infrastructure |
| `issue-3-integration-report.json` | `2026-10-24T18:38:02Z` | `fe6f1271758773dc90ac73975f03763515db7732` | Disposable Synapse on loopback; fake OpenRig CLI | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit fe6f1271758773dc90ac73975f03763515db7732` | Synthetic localhost fixtures; token/room/private-infrastructure scan clean |
| `element-web-verification.txt` | `2026-10-24` | `issue-1-verification-state` investigation | Real Element Web image plus disposable Synapse on loopback | Manual Chrome/agent-browser path recorded inside artifact | No token, room ID, private MXID, recovery data or device private key |
| `issue-11-integration-report.json` | `2026-10-24T19:33:56Z` | `5b70d345b6598f2ca50faf86216f6ba353166ead` | Disposable Synapse on loopback; fake OpenRig CLI | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit 5b70d345b6598f2ca50faf86216f6ba353166ead` | Synthetic localhost fixtures; token/room/private-infrastructure scan clean |
| `issue-12-integration-report.json` | `2026-10-24T19:45:25Z` | `2b50efd164591880bf3c2443c08fdb0fb0dff60e` | Disposable Synapse on loopback; fake OpenRig CLI | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit 2b50efd164591880bf3c2443c08fdb0fb0dff60e` | Synthetic localhost fixtures; token/room/private-infrastructure scan clean |
| `matrix-stability-report.json` | `2026-10-24T19:59:32Z` | `b1e8d17a1d758da3655deb6eb4aba237d2b0ebbc` | Disposable Synapse on loopback; fake OpenRig CLI | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit b1e8d17a1d758da3655deb6eb4aba237d2b0ebbc` | Synthetic localhost fixtures; token/room/private-infrastructure scan clean |
| `issue-4-integration-report.json` | `2026-10-24T22:06:49Z` | `774d1038b61f79fbf28c6709a8d7c5dfbc8dee99` | Disposable Synapse on loopback; fake OpenRig CLI | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit 774d1038b61f79fbf28c6709a8d7c5dfbc8dee99 --report docs/artifacts/issue-4-integration-report.json` | Synthetic localhost fixtures; token/room/private-infrastructure scan clean |
| `final-0.11.0-integration-report.json` | `2026-10-24T22:30:43Z` | `3b21b72330a9b0810457a3ee4d11fdd2c144ae90` | Disposable Synapse on loopback; fake OpenRig CLI | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit 3b21b72330a9b0810457a3ee4d11fdd2c144ae90` | Synthetic localhost fixtures; token/room/private-infrastructure scan clean |
| `final-0.12.0-integration-report.json` | `2026-10-26T04:18:38Z` | `d999efb8b7eb847306f7eabcac66efc6df9e795e` | Disposable Synapse on loopback; fake OpenRig subprocess transport (not live Codex) | `python tools/matrix_integration.py` (37/37 checks, `status: pass`, `evidence_valid: true`, `worktree_clean: true`; three sequential locked runs passed, this is run 3 of 3) | Synthetic localhost fixtures; tokens, room IDs and user IDs omitted |
| `final-0.12.2-integration-report.json` | `2026-10-26T18:32:50Z` | `37aad687dd8dd893d97579611c77a56552a49ac6` | Disposable Synapse on loopback; fake OpenRig subprocess transport (not live Codex) | `python tools/matrix_integration.py` (40/40 checks, `status: pass`, `evidence_valid: true`, `worktree_clean: true`; three sequential locked runs passed, this is run 3 of 3) | Synthetic localhost fixtures; tokens, room IDs and user IDs omitted |
| `final-0.12.1-integration-report.json` | `2026-10-26T10:28:41Z` | `bb58ef6117238ec7ada39536940d53befc269331` | Disposable Synapse on loopback; fake OpenRig subprocess transport (not live Codex) | `python tools/matrix_integration.py` (38/38 checks, `status: pass`, `evidence_valid: true`, `worktree_clean: true`; three sequential locked runs passed, this is run 3 of 3) | Synthetic localhost fixtures; tokens, room IDs and user IDs omitted |

The evidence-only commit follows the tested source commit and is named in the
QA/review handoff. This avoids the impossible self-reference of a commit
containing its own resulting SHA.

Before committing, run the redaction search from
[Verification and evidence](../VERIFICATION.md#redaction-checklist) and record
the observed result here.

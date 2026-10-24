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

Text artifacts are acceptable when they capture real command output. A
screenshot alone is insufficient if the room context is ambiguous.

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

The evidence-only commit follows the tested source commit and is named in the
QA/review handoff. This avoids the impossible self-reference of a commit
containing its own resulting SHA.

Before committing, run the redaction search from
[Verification and evidence](../VERIFICATION.md#redaction-checklist) and record
the observed result here.

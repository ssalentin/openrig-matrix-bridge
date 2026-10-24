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
| `setup-doctor.txt` | The setup and doctor path on disposable Synapse, including failure handling | `b35945549dc913846ea06c4aa7479bd62c0e857e`; report `evidence_valid: true` |
| `room-status.txt` | Real encrypted room, invite/join, topic status, structured status, and avatar | `b35945549dc913846ea06c4aa7479bd62c0e857e`; report `evidence_valid: true` |
| `ordinary-chat.txt` | Encrypted text, typing, qitem history, thread relation, commentary, answer, and FIFO completion gating | `b35945549dc913846ea06c4aa7479bd62c0e857e`; report `evidence_valid: true` |
| `test-path.txt` | Unit, lint, Markdown, link, release, and Matrix integration commands with explicit boundaries | `b35945549dc913846ea06c4aa7479bd62c0e857e` |
| `integration-report.json` | Machine-readable report for all 13 checks with clean source provenance | Source `b35945549dc913846ea06c4aa7479bd62c0e857e`; `worktree_clean: true` |

Text artifacts are acceptable when they capture real command output. A
screenshot alone is insufficient if the room context is ambiguous.

## Manifest

| File | Captured at | Tested source commit | Environment | Command/path | Redaction review |
|---|---|---|---|---|---|
| `integration-report.json` | `2026-10-24T17:19:52Z` | `b35945549dc913846ea06c4aa7479bd62c0e857e` | Disposable Synapse on loopback; fake OpenRig CLI | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit b35945549dc913846ea06c4aa7479bd62c0e857e` | Temporary config path removed; token/room/private-infrastructure scan clean |
| `setup-doctor.txt` | `2026-10-24T17:19:52Z` | `b35945549dc913846ea06c4aa7479bd62c0e857e` | Disposable Synapse; fake OpenRig CLI | Same harness run as `integration-report.json` | Derived from redacted report; no credentials |
| `room-status.txt` | `2026-10-24T17:19:52Z` | `b35945549dc913846ea06c4aa7479bd62c0e857e` | Disposable Synapse; fake OpenRig CLI | Same harness run as `integration-report.json` | Synthetic localhost identities only |
| `ordinary-chat.txt` | `2026-10-24T17:19:52Z` | `b35945549dc913846ea06c4aa7479bd62c0e857e` | Disposable Synapse; fake OpenRig CLI | Same harness run as `integration-report.json` | No IDs or tokens; boundary stated |
| `test-path.txt` | `2026-10-24T17:19:52Z` | `b35945549dc913846ea06c4aa7479bd62c0e857e` | Repository docs/tests plus disposable Synapse | Commands listed inside artifact | No credentials or private infrastructure |

The evidence-only commit follows the tested source commit and is named in the
QA/review handoff. This avoids the impossible self-reference of a commit
containing its own resulting SHA.

Before committing, run the redaction search from
[Verification and evidence](../VERIFICATION.md#redaction-checklist) and record
the observed result here.

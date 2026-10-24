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

| Artifact | What it should prove | Status |
|---|---|---|
| `setup-doctor.txt` | The setup and doctor path on disposable Synapse, including failure handling | Captured; source report pass |
| `room-status.txt` | Real encrypted room, invite/join, topic status, structured status, and avatar | Captured; source report pass |
| `ordinary-chat.txt` | Encrypted text, typing, fake OpenRig delivery, qitem lifecycle, commentary, answer, and FIFO queue | Captured; source report pass |
| `test-path.txt` | Unit, lint, Markdown, link, and Matrix integration commands with explicit boundaries | Captured; commit metadata supplied in handoff |
| `integration-report.json` | Machine-readable report for all 13 checks; temporary config paths are redacted and `environment.command` reflects the engine-reported actual invocation | Captured; source report pass |

Text artifacts are acceptable when they capture real command output. A
screenshot alone is insufficient if the room context is ambiguous.

## Manifest template

Add one row per committed artifact:

| File | Captured at | Commit | Environment | Command/path | Redaction review |
|---|---|---|---|---|---|
| `<name>` | `<UTC timestamp>` | `<sha>` | `<real Synapse / fake OpenRig / manual>` | `<exact invocation>` | `<reviewer initial + date>` |

Before committing, run the redaction search from
[Verification and evidence](../VERIFICATION.md#redaction-checklist) and record
the observed result here.

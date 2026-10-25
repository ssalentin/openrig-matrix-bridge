# Release 0.11.0 plan

Owner authorization, the milestone, and the final scope decision are recorded
in [GitHub issue #8](https://github.com/ssalentin/openrig-matrix-bridge/issues/8).
The Engineering Lead retains final merge, tag, asset upload, and GitHub Release
authority.

## Frozen outcome

0.11.0 ships from a fast-forward descendant of `main` and includes:

- encrypted arbitrary-file attachments in both directions;
- in-place voice-note status updates for transcript and failure outcomes;
- canonical threaded delivery for prompts already inside a Matrix thread;
- restart-aware prompt, answer, attachment, verification and qitem effects;
- safe Matrix endpoint and agent-path validation;
- redacted and recoverable live activity cards;
- deterministic release validation, documented setup, and Relay identity; and
- an executable installation, verification and rollback runbook.

The release does not claim hybrid steering, queue previews, additional setup
automation, a duplicate-card fix, legacy pending-state reconciliation, or
real-device SAS/cross-signing. Those items are deferred to milestone 0.12.0.

## Integration ancestry

The release branch starts from `main` at
`e152c30f49185bfb42e0a6007ce072c2701add0d`, which is merged into the release
history before feature integration. The frozen pre-release base is
`a0551cb8cadd595d6d1012cab1e7eaf7c2c3b13c`.

Only accepted runbook commits are layered on that base. Version and release
documentation are committed after the scope freeze. The final approved SHA must
be a fast-forward target for `main`; no merge commit may be added after its QA,
review and cross-model audit.

## Accepted evidence

| Area | Evidence |
|---|---|
| Attachments | Encrypted PDF, unknown binary and markerless audio in both directions; private snapshots and restart recovery. |
| Voice | One status event edited to transcript, no-speech or terminal failure; restart and send-failure recovery. |
| Threads | Canonical input roots, threaded text/media, restart preservation and bounded delivery failure. |
| Restart/queue | Stable prompt/answer/qitem phases, busy and claim recovery, ambiguity fail-closed behavior and qitem reconciliation. |
| Configuration | HTTPS/token/path safety and generated-key bounds. |
| Activity card | Redaction, ordered refresh and terminal delivery recovery. |
| Release | Unit, lint, Markdown, links, wheel/sdist, clean installs, exact-source Matrix integration and CI. |

Historical issue-specific raw reports remain under `docs/artifacts/`.

## Deferred scope

Milestone 0.12.0 owns:

- #1 real-device verification and cross-signing strategy;
- #4 hybrid queue/steering and #36 post-grace duplicate risk;
- #24 pending-prompt previews in the queue card;
- #37 minimal-setup automation and instance locking;
- #39 duplicate activity-card race; and
- #41 reconciliation of pre-0.11 pending state with terminal qitems.

PRs #27, #34, #35 and #40 are deferred with their issues. Their branches may
remain as implementation evidence but must not enter the 0.11.0 artifacts.

## Release gates

Run at the exact candidate source SHA in a clean worktree with an isolated
environment:

```bash
python -m pytest -q
ruff check .
python tools/check_markdown_links.py
npx --no-install markdownlint-cli2
python -m build
python tools/verify_release.py
python tools/matrix_integration.py \
  --timeout 60 \
  --source-commit "$(git rev-parse HEAD)"
```

Also:

- inspect wheel and sdist contents for only tracked release material;
- clean-install both artifacts and verify the console entry point, 32 avatars,
  Relay assets and package metadata;
- scan artifacts and notes for secrets, private MXIDs, homeserver details and
  recovery material;
- confirm GitHub CI is green at the exact final SHA;
- obtain independent QA and code/release review at that SHA; and
- obtain the required cross-model Fable audit at that SHA.

Any source or documentation commit after approval invalidates all verdicts.

## Required handoff

- frozen issue and deferred-PR dispositions;
- release branch and immutable candidate SHA;
- exact commands, counts, report paths and CI links;
- wheel and sdist paths with SHA-256 checksums;
- independent QA, review and Fable verdicts;
- truthful release notes and known limits;
- the explicit statement that engineering performs the final merge, `v0.11.0`
  tag, asset attachment and GitHub Release publication.

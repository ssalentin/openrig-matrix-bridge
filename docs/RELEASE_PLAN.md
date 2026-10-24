# Release 0.11.0 plan

Owner authorization and scope are recorded in
[GitHub issue #8](https://github.com/ssalentin/openrig-matrix-bridge/issues/8).
The base is `771979544ced46ae2fb0a99b2c49fddf1cc54666` on `main`. The
Engineering Lead retains final merge, tag, and GitHub Release authority. This
cell prepares and proves the release candidate only.

## Release outcome

Ship `0.11.0` with:

- arbitrary encrypted file attachments in both directions;
- one voice-note status message edited into the terminal transcript or error;
- thread-aware hybrid queue/steering with durable fallback and one active turn;
- an honest resolution of Matrix device-verification and cross-signing work;
- recorded Element X threaded-reply compatibility evidence;
- release validation and documentation improvements found by the audit.

Every milestone issue must be closed or have a written, evidence-backed owner
disposition before handoff. No claim may exceed the committed unit, integration,
or manual evidence behind it.

## Baseline evidence

The independent lead audit was run at the base commit:

| Check | Observed result |
|---|---|
| `.venv/bin/python -m pytest -q` | 74 passed |
| `.venv/bin/ruff check .` | All checks passed |
| `npx --yes markdownlint-cli2` | 5 files, 0 issues |
| Relative Markdown links | checked 5 files, 0 missing |
| `.venv/bin/python -m build` | wheel and sdist built for 0.10.0 |
| `.venv/bin/python tools/verify_release.py` | wheel and sdist clean-installed; 32 avatars present |
| Matrix integration harness with `--source-commit` | pass, `evidence_valid: true`, 13 checks |

The integration source provenance was clean. Known baseline boundaries are the
fake OpenRig transport, no Parakeet execution, no real Element device SAS run,
and no public-homeserver or federation test.

## Audit disposition

The lead audit found one additional release-integrity gap: documentation checks
were documented but not enforced in CI, and release-artifact verification did
not assert all published evidence documents. It is tracked as issue #9.

Independent Phase 0 QA confirmed the baseline and found three release-critical
areas outside the original feature scopes:

- #11 makes prompt, answer, attachment, and qitem remote effects restart-safe;
- #12 prevents qitem lifecycle failures from silently diverging from README
  claims;
- #13 rejects plaintext non-loopback Matrix transport and unsafe agent path
  components.

QA also confirmed that inbound thread roots must be preserved. That requirement
belongs to #4 and has been added to its acceptance record.

No implementation work may be folded into another issue's review boundary.
Feature branches start from the release base, remain independently reviewable,
and are integrated into `release/0.11.0` only after their focused tests pass.

## Work breakdown

| Issue | Branch | Primary ownership | Principal files | Dependencies |
|---|---|---|---|---|
| #5 | `issue-5-voice-status-edit` | Interface | `bridge.py` voice handler, `tests/test_bridge.py`, voice docs | base |
| #3 | `issue-3-encrypted-attachments` | Core | `media.py`, `bridge.py` media paths, `config.py`, `cli.py`, integration harness | base |
| #9 | `issue-9-release-validation` | Core + Interface | release verifier, CI, release tests, docs | plan |
| #11 | TBD | Core | request state machine, outbox/state recovery, fault-injection harness | #3 and #5 integrated |
| #12 | TBD | Core | qitem lifecycle and gateway error handling, fake transport faults | #3 and #5 integrated |
| #13 | TBD | Interface | config URL/key validation and media path containment | base |
| #4 | `issue-4-hybrid-steering` | Core | queue/request lifecycle in `bridge.py`, transcript attribution, config and harness | #3 and #5 integrated |
| #1 | `issue-1-device-verification` | Interface + QA | SAS state machine and setup/doctor guidance, tests and manual evidence | real-client availability |
| #7 | `issue-7-element-x-evidence` | Interface + QA | `docs/VERIFICATION.md`, redacted manual artifacts | owner screenshot or honest gap |
| #8 | `release/0.11.0` | Project Lead + QA + Review | version, changelog, README, evidence, release artifacts | all above |

The first two feature branches can proceed in parallel because voice and media
entry points are separate. They both touch `bridge.py`, however, so the release
integrator preserves each commit boundary and resolves shared helper changes
explicitly. Hybrid steering starts only after both are integrated because it
changes the common request lifecycle and transcript attribution.

## Acceptance and risk

### Voice status editing

Tests must prove one status send followed by edits for success, no speech,
transcription failure, and edit rejection; the original voice event remains the
thread root; restart cannot duplicate the transcript.

### Attachments

Tests must prove encrypted PDF and unknown-binary round trips, original metadata
and bytes, generated storage paths, path-traversal resistance, zero-byte and
oversized rejection, malformed/interrupted failure states, and outbound restart
recovery. Existing image behavior must remain unchanged.

### Hybrid steering

Tests must prove same-thread rapid batching, top-level and cross-thread isolation,
`queue`/`steer`/`auto` semantics, one active turn, lossless queue fallback on a
steering race or unavailable boundary, durable qitem consistency, one answer for
a batch, and restart recovery without duplicate or stolen transcript answers.

### Device verification and client evidence

Code tests may prove the verification state machine and failure paths. A mock is
not evidence that a real device completed SAS, that a verified-device warning
disappeared, or that Element rendered a threaded reply. If the environment
cannot perform the physical/client action, the release records that exact gap
and seeks an explicit owner disposition for the affected criterion.

### Release candidate

Independent QA and review must approve the same full candidate SHA. Any later
commit invalidates both verdicts. The final candidate must build and clean-install
the wheel and sdist, prove the console entry point and avatar assets, run the
Matrix integration harness against that SHA with clean tracked provenance, and
pass the redaction checklist.

## Integration strategy

1. Each feature branch opens a focused pull request against `main`.
2. Accepted feature commits are cherry-picked in dependency order onto
   `release/0.11.0`; no feature PR is merged by this cell.
3. The version bump and release-document edits happen only after feature
   behavior is accepted.
4. The release branch becomes the immutable candidate named in the QA, review,
   artifact, CI, and Engineering Lead handoffs.
5. No merge, tag, asset upload, or GitHub Release publication is performed by
   this cell.

## Release gates

Run these at the exact candidate SHA:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
npx --yes markdownlint-cli2
.venv/bin/python -m build
.venv/bin/python tools/verify_release.py
.venv/bin/python tools/matrix_integration.py \
  --timeout 60 \
  --source-commit "$(git rev-parse HEAD)"
```

Also run the documented relative-link check and redaction scan, inspect current
CI rather than relying on old evidence, and record any unavailable real-client
or physical-device check as an explicit limit.

## Required handoff

- all milestone issue dispositions with links;
- feature PRs and integrated commits;
- release branch and immutable candidate SHA;
- exact commands, results, CI URL, and integration report;
- wheel and sdist paths with checksums;
- independent QA and review verdicts for the candidate;
- truthful release notes and known limits;
- no secrets, private Matrix identifiers, homeserver details, recovery material,
  or unredacted artifacts.

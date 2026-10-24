# OpenRig Matrix Bridge 0.11.0

OpenRig Matrix Bridge 0.11.0 focuses on safer media handling, durable recovery
and canonical Matrix threads. It keeps OpenRig as the agent runtime and gives
each configured Codex seat a private, encrypted Matrix room without starting a
second agent.

## Highlights

- Send encrypted PDFs, archives, office documents and other files in either
  direction. Admitted outbound files use a private byte snapshot, so retries do
  not read a changed source path.
- Voice-note status is edited in place to the local transcript, no-speech result
  or explicit failure.
- Prompts already inside a Matrix thread keep their canonical root, and
  commentary and answers remain in that thread.
- Prompt, answer, attachment and OpenRig queue effects are restart-aware.
  Requests whose prompt-send effect became ambiguous fail closed instead of
  being replayed.
- Configuration rejects unsafe Matrix endpoints and agent path components.
- The accessible setup guide explains the existing OpenRig and Matrix path
  without moving credentials into YAML or repository files.
- Relay is the selected 0.11.0 symbol; the product name remains OpenRig Matrix
  Bridge.

## Verification and upgrade

The release is tested with unit, lint, documentation, link, wheel/sdist and
clean-install gates plus a disposable Synapse Matrix integration harness. The
integration harness uses a deterministic OpenRig transport rather than a live
Codex model.

Use the [0.11.0 runbook](RUNBOOK_0.11.0.md) to verify the exact release asset,
confirm one bridge process, run live plain, multi-line, attachment and threaded
prompt smoke tests, and roll back safely if required.

## Known limits

- Only Codex seats are supported.
- Real Element device SAS, unverified-device warning removal and automatic
  cross-signing remain unverified and are deferred to milestone 0.12.0.
- Element X threaded replies were owner-observed, but the repository has no
  redacted screenshot or client-version record.
- Real Parakeet recognition quality and all audio formats remain manual gaps.
- Federation, public-homeserver policy and production device trust are not
  exercised by the automated harness.
- Arbitrary files are not malware-scanned or previewed.

These limits are not claims of success. See
[Verification and evidence](VERIFICATION.md) for the exact boundary.

## Deferred to 0.12.0

- Hybrid queue/steering for follow-up Matrix messages (#4), including the
  duplicate-execution race tracked by #36.
- Ordered previews of pending prompts in the pinned queue card (#24).
- Additional minimal-setup automation and instance locking (#37).
- A reproducible fix for the intermittent duplicate activity-card race (#39).
- Reconciliation and migration of pre-0.11 pending state whose qitems are
  already terminal (#41).
- Real-device SAS verification and cross-signing strategy (#1).

These changes are not part of the 0.11.0 wheel or source distribution.

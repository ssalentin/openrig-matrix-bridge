# Cross-signing evaluation

Issue [#1](https://github.com/ssalentin/openrig-matrix-bridge/issues/1) asks
whether a `matrix-nio` or other SDK upgrade could let the bridge take part in
Matrix cross-signing without weakening the trust model. This note records the
facts checked on 2026-10-25 and the decision. It changes no dependency.

## Today

- The bridge pins `matrix-nio[e2e]>=0.25,<0.26`; the lock file resolves
  `0.25.2` (2024-10-04).
- `matrix-nio` 0.25.2 contains no cross-signing code: no master, self-signing,
  or user-signing keys, and no device-signing or signature upload.
- Trust is therefore per device. Each owner device verifies the bridge device
  directly with the emoji SAS flow
  ([Matrix device verification](SECURITY.md#matrix-device-verification)).
  matrix-nio records each verified device in its own trust file, and
  `openrig-matrix-bridge doctor` lists them.
- Element shows the bridge account as "not verified by its owner", because the
  bridge user has no cross-signing identity. A successful SAS removes the
  warning for that device pair only. That removal is not proven by this
  repository; it needs the real-device check in issue #1.

## Upgrade options

| Option | Cross-signing | Status on 2026-10-25 | Verdict |
|---|---|---|---|
| `matrix-nio` 0.26.0 (2026-07-23, latest release) | No: the project README still lists cross-signing as unsupported; tracking issue [#229](https://github.com/matrix-nio/matrix-nio/issues/229) has been open since 2020 | Two open regressions break the bridge's existing emoji SAS: [#570](https://github.com/matrix-nio/matrix-nio/issues/570) (the SAS commitment is sent as hex, so Element cancels with `m.key_mismatch` before any emoji appear) and [#567](https://github.com/matrix-nio/matrix-nio/issues/567) (verification requests are not delivered after the vodozemac migration). 0.26.0 also drops the `hmac-sha256` SAS MAC | **Do not upgrade.** Keep the `<0.26` pin |
| `matrix-nio` PR [#579](https://github.com/matrix-nio/matrix-nio/pull/579) "Add cross-signing and Secret Storage (SSSS) support" | Yes, when merged: recovery-key import, `sign_own_device()`, `bootstrap_cross_signing()`, `verify_user()` | Open and unmerged; CI failing; not released | Re-evaluate once it is released and #570/#567 are fixed |
| `mautrix-python` ≥ 0.21.0 (2025-11-17) | Generates and uses recovery keys to verify the active device; its bridges offer a "self-sign bot device" option | Released | A different SDK, crypto store, and client API: a migration of the whole Matrix layer, not an upgrade. Out of scope; would need its own issue |

## Trust-model conditions for any future cross-signing

Self-signing gives the bot a cross-signing identity. Each new bot device is
then trusted wherever the bot user is trusted. That is safe only if:

1. **The owner verifies the bot user once, interactively** (user-signing with
   the owner's own cross-signing keys). Without that step, self-signing only
   says "the bot vouches for itself" and adds no owner trust.
2. **The bot's recovery key and private cross-signing keys are protected at
   least like the access token.** Anyone holding them could sign a rogue device
   that every owner device would then trust automatically. Today an attacker
   would need to pass a new SAS on each owner device.
3. **No recovery key is generated into logs.** A generated key must be written
   once to a private (`0600`) file or supplied by the operator. Printing it to
   the service log, as some bridge examples do, would weaken the model.
4. **A changed bot master key is surfaced to the owner** and never re-trusted
   silently.

## Decision

Keep `matrix-nio` 0.25.2 and per-device emoji verification for 0.12.0. Do not
claim cross-signing. Revisit when a `matrix-nio` release contains cross-signing
(PR #579 or a successor) and fixes #570 and #567, then implement against the
four conditions above with its own tests and a real-device check.

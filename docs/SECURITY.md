# Security and trust

The bridge keeps message content inside Matrix end-to-end encryption and local
OpenRig state. This page states the trust model and the boundaries the
repository currently proves.

## Security and E2EE

- New rooms are private and created with `m.megolm.v1.aes-sha2` encryption.
- Only one owner is invited when rooms are created. Messages from senders who
  are not listed in `matrix.allowed_users` are ignored.
- The access token is read from `MATRIX_ACCESS_TOKEN`; it is not stored in YAML.
- Encrypted image and generic-file uploads are encrypted before upload. Remote
  Markdown images are shown as references and are not embedded or fetched.
- Voice audio is processed locally after the model is downloaded. The model
  archive is verified against a pinned SHA-256 checksum.
- State, the Matrix encryption store, and media inbox contain sensitive data.
  Keep the state directory private (`0700` directories and `0600` files) and
  back it up securely.
- While an inbound image, file, or gallery item is downloading, `state.json`
  holds its Matrix media URL and attachment decryption key so a restart can
  resume it. The bridge removes them once the item is stored or has failed;
  treat a `state.json` copy taken mid-download as containing those keys.
- Losing the Matrix encryption store can make previously encrypted history
  unreadable even if the room still exists.
- Matrix homeserver administrators can still see room membership and message
  metadata, even when message content is encrypted.

Do not commit tokens, device private keys, recovery material, raw encryption
state, unredacted private MXIDs, room IDs, homeserver addresses, or media.

## Matrix device verification

The first connection from a new client may show an unverified bot device. The
bridge can handle an incoming device-to-device emoji SAS request and persists
the transaction phase so a restart cannot turn `ja` or `nein` into an agent
prompt:

1. In Element, open the bot device details and start **Verify by emoji**.
2. The bridge posts seven emojis in the `Advisor` room, or another configured
   room if no `Advisor` room exists.
3. Compare them with Element and click **They match**.
4. Type `ja` in that Matrix room to confirm the bridge side.

After an interrupted verification, plain `ja` and `nein` remain reserved in
that room so they cannot accidentally become agent prompts. To intentionally
send ordinary confirmation text to the agent, use an explicit `agent:` prefix,
for example `agent: ja`.

Verification applies to the specific Element device. `matrix-nio` does not
currently provide complete cross-signing setup, so the bridge does not create a
cross-signed identity that Element automatically trusts across devices. In the
2026-10-24 Element Web investigation, Element showed
`(User verification unavailable)` for the bridge account even with a synthetic
client and disposable homeserver. Treat direct SAS as unverified until a
real-client run records success; do not infer it from unit tests.

Live Element SAS verification is not covered by the automated test suite.
Restart interruption, timeout, cancellation and confirmation state are
unit-tested. Interruption and timeout notifications persist an outbox intent
and use a stable Matrix transaction ID so startup reconciliation retries them
exactly once. The real-client flow and cross-signing limitation remain tracked
in [Issue #1](https://github.com/ssalentin/openrig-matrix-bridge/issues/1); do
not describe them as fully verified until a real client check is recorded.

The committed Element Web screenshots are synthetic demo evidence for visual
rendering. They do not prove device verification or cross-signing. See
[screenshot provenance](screenshots/README.md) for the capture boundary.

## What this does not do yet

- It supports Codex seats only, not every OpenRig runtime.
- Matrix may still show an unverified-device warning. Verification is per
  device; the bridge does not provide complete cross-signing for every owner
  device.
- Real Parakeet recognition quality and live Element rendering remain manual
  evidence, not automated guarantees.
- A stopped seat can remain configured, but it cannot accept prompts until it
  is running again.

See the [evidence matrix](VERIFICATION.md) for the precise proof behind each
claim.

## Known limits

- Only Codex seats are supported. Other OpenRig runtimes are out of scope.
- Element X threaded replies were manually observed by the owner on 2026-10-24,
  but the repository does not have a versioned Element X capture. The real
  Matrix harness verifies canonical roots, including a prompt already inside a
  thread; it does not prove every Element X nested-reply UI state.
- Live Element SAS and the broader cross-signing strategy are not fully
  verified by the repository; Element Web currently reports verification
  unavailable for the bridge account. See Issue #1.
- A mocked Matrix client is not a substitute for the committed integration run.
- Voice transcription quality and every audio format are not covered by a
  deterministic end-to-end test; model readiness and checksum configuration
  are unit-tested.
- OpenRig status is polled, not a push subscription. Very brief state changes
  can be missed between refreshes.
- Ordinary terminal chat is not mirrored to Matrix.
- The bridge supports one invited owner by default. Additional accounts must
  be intentionally added to the room and allowlist.
- Inline image support is limited to PNG, JPEG, GIF, and WebP. Other file types
  use downloadable `m.file` attachments subject to `media.max_file_bytes`.
- Arbitrary files are not malware-scanned, previewed, or fetched from remote
  URLs. Inbox retention and redaction of local paths remain operator duties.
- The default user-facing status and help strings are German.

## Unsupported claims

Do not present the current implementation as:

- verified with Element after a real device SAS flow unless that evidence is
  recorded;
- cross-signed or automatically trusted on every device;
- fully tested end to end while coverage is unit tests plus mocks;
- usable from any Codex conversation on any device without checking device
  trust and the one-owner allowlist;
- production-quality voice for every audio format or language;
- invisible to the homeserver, since E2EE protects content while Matrix
  metadata remains visible;
- zero-downtime or generically restart-safe without the committed restart
  evidence.

## Redaction checklist

Review every artifact before commit:

```bash
rg -n -i \
  'access[_ -]?token|password|secret|recovery[_ -]?key|private[_ -]?key|@[^ ]+:[^ ]+|![^ ]+:[^ ]+|mxc://[^ ]+' \
  docs/artifacts docs/screenshots
```

The check should produce only intentional synthetic values such as
`@owner:matrix.example.org`. Inspect images manually because text search cannot
detect a room name drawn in a screenshot or a notification preview.

Do not retain raw `state.json`, Matrix crypto stores, user/device keys, access
tokens, password-manager output, private homeserver logs, or unredacted
timeline exports.

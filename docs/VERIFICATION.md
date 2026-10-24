# Verification and evidence

This note separates what the repository proves from what merely looks plausible
in its implementation. It is the source for product positioning and launch
copy. Update it with the observed command, commit, environment, and artifact
path whenever a manual or integration check is performed.

## Evidence levels

| Level | Meaning |
|---|---|
| Automated unit | The implementation path runs against a mocked Matrix/OpenRig boundary. Useful for contracts and failure handling, not proof of a live server. |
| Automated integration | The committed harness starts or connects to a real Matrix homeserver and exercises the public bridge path. The exact harness boundary and retained artifact must be named. |
| Manual | A person or documented harness performed the check in a named environment. The result and limitations must be recorded in an artifact. |
| Unverified | The repository does not currently prove the claim. It must not appear as a launch promise. |

## Provenance

The clean combined source under test is
`88dccb3c92329918c62b68f871022d0972377cd8`. It contains the product base
`78ae4be5128d632a0b1b6df5ac4b0c3afc69cb0d`, the engine evidence fix
`aac588a16219736d34cf7b16e04ca2474b0838a8`, the engine test portability fix
`bda0e5d`, and the positioning correction.
The integration report records this source commit and
`target.source.worktree_clean: true`.

The commit that publishes these evidence edits follows the tested source
commit. A commit cannot contain the hash it will derive after changing its own
contents, so the QA/review handoff names that evidence commit explicitly and
binds its verdict to the source SHA recorded here and in the report.

## Product claim matrix

| Product claim | Implementation | Automated evidence | Live/reproducible evidence | Current status |
|---|---|---|---|---|
| One private room per configured agent and no duplicate agent process | [room creation](../src/openrig_matrix_bridge/bridge.py) is `_create_rooms()`; [configuration](../src/openrig_matrix_bridge/config.py) rejects duplicate sessions; prompts use `rig send --raw` against the resolved live seat rather than a second runtime. | [`test_rejects_duplicate_sessions`](../tests/test_config.py#L57), [`test_sends_prompt_through_rig_cli_with_bridge_identity`](../tests/test_openrig.py#L100), [`test_rejected_agent_room_is_not_recreated`](../tests/test_bridge.py#L280). | Disposable Synapse accepted one invited room; restart preserved the mapping without creating a duplicate. The harness covered one configured agent, so multi-agent cardinality remains unit/config evidence. | **Verified for one agent end to end; multi-agent cardinality unit-tested.** |
| Live status and typing | Status is published to `m.room.topic` and `dev.openrig.agent_status`; typing is refreshed by `_typing_loop()` and cleared when a request finishes. | [`test_status_uses_topic_and_structured_event_instead_of_room_name`](../tests/test_bridge.py#L122). | The owner client observed the ready topic, structured status, and bridge typing indicator on disposable Synapse. | **Verified on live Matrix protocol path; no Element screenshot required for this claim.** |
| End-to-end encrypted text and room media | `AsyncClientConfig(encryption_enabled=True)` stores E2EE state; newly created rooms include Megolm encryption; image uploads request encryption and verify encryption metadata. | [`test_encrypted_image_is_saved_and_forwarded_to_agent`](../tests/test_bridge.py#L1111), [`test_builds_encrypted_image_message`](../tests/test_media.py#L15). | Synapse reported `m.megolm.v1.aes-sha2`; owner text and image traffic was encrypted through matrix-nio and decrypted back. | **Verified on disposable Synapse; public-server and device-trust policy remain gaps.** |
| Canonical threaded prompt delivery | Inbound `m.thread` roots are preserved; top-level prompts start one request thread; rich replies without a thread use `m.in_reply_to` instead of manufacturing a thread. Commentary and final delivery fail boundedly rather than blocking later requests. | Relation, storage, delivery, and bounded-failure tests in [`test_bridge.py`](../tests/test_bridge.py). | An encrypted owner prompt already inside `m.thread` survived a bridge restart while the fake seat was busy; commentary and final answer each appeared once under the original root and the qitem closed. | **Verified on disposable Synapse with fake OpenRig transport.** |
| Local speech-to-text | `Transcriber` invokes local `ffmpeg` and `sherpa-onnx`; model readiness and checksum are implemented. No audio is sent to a transcription API. | [`test_model_state_checks_all_required_files`](../tests/test_transcription.py#L19), [`test_parakeet_checksum_is_pinned`](../tests/test_transcription.py#L28), [`test_audio_suffix_is_preserved`](../tests/test_transcription.py#L9). | Encrypted audio reached the bridge and the disabled-voice response passed. The 465 MB model and recognition quality were not run. | **Disabled path verified; real transcription unverified.** |
| Encrypted image transfer in both directions | Incoming images are downloaded, decrypted, type-checked, size-limited, stored, and forwarded as a path. Outgoing `send-image` encrypts uploads and emits an encrypted image event. | [`test_encrypted_image_is_saved_and_forwarded_to_agent`](../tests/test_bridge.py#L1111), [`test_detects_supported_image_signatures`](../tests/test_media.py#L7), [`test_builds_encrypted_image_message`](../tests/test_media.py#L15). | Owner PNG was stored byte-for-byte and forwarded; `send-image` produced an image the owner downloaded and decrypted byte-for-byte. | **Verified on disposable Synapse.** |
| Encrypted arbitrary-file transfer in both directions | Incoming `m.file` events are size-checked, decrypted, stored under generated paths, and forwarded with original metadata. Outgoing `send-file` encrypts uploads, emits `m.file`, and persists restart-safe delivery state. | [`test_encrypted_pdf_is_safely_stored_and_forwarded_to_agent`](../tests/test_bridge.py), [`test_publishes_encrypted_generic_file_with_original_metadata`](../tests/test_bridge.py), [`test_encrypted_file_write_failure_leaves_no_partial_file`](../tests/test_bridge.py), [`test_builds_encrypted_generic_file_message`](../tests/test_media.py). | The committed harness adds PDF and unknown-binary transfers in both directions plus a stopped-bridge outbox recovery check. | **Unit-verified; exact Synapse result is bound in the engine handoff.** |
| Pinned decisions and Element-compatible polls | Decisions are written to a durable inbox, published as a pinned brief plus MSC3381 poll events, and selected answers are returned as a queued prompt. | [`test_poll_answer_is_forwarded_and_pinned_decision_is_cleared`](../tests/test_bridge.py#L327), [`test_builds_element_compatible_poll_content`](../tests/test_decisions.py#L36), [`test_reads_unstable_poll_response`](../tests/test_decisions.py#L55). | Brief and poll were pinned; encrypted response returned to the fake transport; completed events were unpinned. | **Protocol path verified; Element rendering not verified.** |
| Reactions as silent feedback | Owner reactions to bridge-authored events are resolved, bounded, forwarded to the agent with `suppress_reply=True`, and excluded from visible answer delivery. | [`test_reaction_to_bot_post_becomes_silent_feedback`](../tests/test_bridge.py#L468), [`test_silent_feedback_does_not_post_agent_answer`](../tests/test_bridge.py#L523). | Owner reaction reached the fake transport as feedback and created no visible bot reply on Synapse. | **Verified at fake-transport/Matrix boundary.** |
| Durable OpenRig queue behavior | Each tracked Matrix input creates a priority qitem with `--no-nudge`, claims it when work starts, closes it after answer delivery, and leaves untracked reactions out of queue creation. | [`test_creates_matrix_qitem_with_priority_and_no_duplicate_nudge`](../tests/test_openrig.py#L159), [`test_claims_and_finishes_qitem_as_destination_agent`](../tests/test_openrig.py#L205), [`test_matrix_message_creates_priority_qitem`](../tests/test_bridge.py#L953). | A Matrix prompt created an urgent fake qitem, moved pending to in-progress to done, and was delivered once. | **Command contract verified; live OpenRig daemon recovery unverified.** |
| FIFO prompt queue and pinned queue card | Per-agent `asyncio.Lock` and pending lists serialize prompts; one queue notice is edited and unpinned as the count changes. | [`test_queue_notice_is_one_updated_and_pinned_message`](../tests/test_bridge.py#L904). | While the first fake turn was held, a second prompt appeared in the pinned queue notice and the transport received both in FIFO order. | **Verified on disposable Synapse with fake transport.** |
| Markdown and safe rich-text rendering | Markdown becomes Matrix HTML; raw HTML is escaped; remote images are rendered as references instead of fetched. | [`test_renders_gfm_table_as_matrix_html`](../tests/test_formatting.py#L4), [`test_escapes_raw_html`](../tests/test_formatting.py#L14), [`test_does_not_embed_remote_markdown_images`](../tests/test_formatting.py#L21). | Client rendering is not automated. Matrix event content can be captured. | **Unit-verified.** |
| Deterministic, collision-free agent avatars | A bundled 32-image pool is assigned once per agent and persisted in state. | [`test_ships_a_large_unique_avatar_pool`](../tests/test_avatar_pool.py#L10), [`test_preserves_valid_assignments_and_never_duplicates`](../tests/test_avatar_pool.py#L20), [`test_new_agents_receive_unused_avatars_deterministically`](../tests/test_avatar_pool.py#L35). | Synapse accepted a managed room avatar on the test room. | **Unit-verified and observed on Synapse.** |

## Current automated checks

Run from the repository:

```bash
python -m pytest -q
python -m pip install ruff
ruff check .
```

Observed results from the clean combined source:

| Check | Exact command | Commit | Observed result |
|---|---|---|---|
| Unit tests | `.venv/bin/python -m pytest -q` | `88dccb3c92329918c62b68f871022d0972377cd8` | 74 passed in 0.38s |
| Python lint | `.venv/bin/ruff check .` | `88dccb3c92329918c62b68f871022d0972377cd8` | All checks passed |
| Markdown structure | `npx --yes markdownlint-cli2` | `88dccb3c92329918c62b68f871022d0972377cd8` | 5 files, 0 issues |
| Relative links | Documentation link-check command below | `88dccb3c92329918c62b68f871022d0972377cd8` | checked=5, missing=0 |
| Release artifacts | `.venv/bin/python -m build` then `.venv/bin/python tools/verify_release.py --dist <dir>` | `88dccb3c92329918c62b68f871022d0972377cd8` | Wheel and sdist clean installs passed; 32 avatars present |
| Matrix integration | `.venv/bin/python tools/matrix_integration.py --timeout 60 --source-commit 88dccb3c92329918c62b68f871022d0972377cd8` | `88dccb3c92329918c62b68f871022d0972377cd8` | `evidence_valid: true`, 13 checks passed in 24.2s |

With `uv` installed, `uv tool run ruff check .` is equivalent to the lint
command above.

## Documentation checks

Check every relative Markdown link from the repository root:

```bash
python3 - <<'PY'
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

root = Path.cwd()
files = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
missing = []
for path in files:
    for target in re.findall(r"(?<!!)\[[^]]*\]\(([^)]+)\)", path.read_text()):
        target = target.strip().strip("<>")
        parts = urlsplit(target)
        if parts.scheme or target.startswith("#"):
            continue
        candidate = (path.parent / unquote(parts.path)).resolve()
        if not candidate.exists():
            missing.append(f"{path.relative_to(root)}: {target}")
print(f"checked={len(files)} missing={len(missing)}")
for item in missing:
    print(f"MISSING {item}")
raise SystemExit(bool(missing))
PY
```

Check Markdown structure with the repository's narrow lint policy:

```bash
npx --yes markdownlint-cli2
```

Run the artifact redaction check from the
[redaction checklist](#redaction-checklist), and inspect binary artifacts
manually.

## Integration boundary

The engine slice provides `tools/matrix_integration.py`. It starts disposable
Docker Synapse and drives the bridge as a subprocess through a deterministic
OpenRig CLI fixture. Its JSON report is
`docs/artifacts/integration-report.json` (schema version 1).

The observed run used:

```bash
.venv/bin/python tools/matrix_integration.py \
  --timeout 60 \
  --source-commit 88dccb3c92329918c62b68f871022d0972377cd8
```

It reported `status: pass` with 13 checks at
`2026-10-24T17:22:44Z` in 24.2 seconds, with
`target.source.commit` equal to the tested source SHA and
`target.source.worktree_clean: true`. The digest-pinned Synapse image is
recorded in the report. The owner client was a real `matrix-nio` client; the
homeserver was loopback-only; the OpenRig transport was fake.

The harness boundary intentionally does not prove:

- production OpenRig daemon behavior or queue recovery;
- a live Codex model turn;
- local Parakeet recognition quality;
- Element rendering or human SAS/cross-signing.

Those are manual checks or explicit release gaps, not mocked successes. The
redacted evidence excerpts are in `docs/artifacts/`.

## Required live/manual matrix

Keep these rows open until evidence is actually committed:

| Scenario | Expected observation | Evidence |
|---|---|---|
| Setup and doctor | Private config is created; token presence and Matrix `whoami` are checked without exposing the token. | Pass on disposable Synapse; `setup-doctor.txt` |
| Agent room creation and invite | Private encrypted room is created once and the owner receives an invite. | Pass on disposable Synapse; `room-status.txt` |
| Ordinary chat | Prompt reaches the same configured seat; typing/status change; final answer arrives in a thread. | Matrix path pass with fake OpenRig; live Codex remains manual; `ordinary-chat.txt` |
| Threaded prompt | Prompt already in `m.thread` keeps its canonical root across commentary, final answer, a bridge restart, and qitem closure. | Pass on disposable Synapse with fake OpenRig; `threaded-prompt-restart` |
| FIFO queue | Second prompt waits; pinned count updates; answers remain ordered. | Pass with fake transport; `ordinary-chat.txt` |
| Status and typing | Topic switches `bereit`/`arbeitet`; typing is visible while working. | Pass on disposable Synapse; `room-status.txt` and `ordinary-chat.txt` |
| Image exchange | Incoming encrypted image is decrypted to the media inbox; outgoing image is encrypted. | Pass byte-for-byte on disposable Synapse |
| Arbitrary-file exchange | PDF and unknown binary preserve bytes, filename, MIME type, size, safe storage, and encrypted Matrix events; restart recovery delivers once. | Automated Synapse harness covers both directions and pending outbox recovery |
| Voice note | Local model transcribes a short sample, posts text, and forwards it. | Encrypted disabled-voice path passed; model execution is an explicit skipped gap |
| Decision/poll | Brief and poll are pinned; selection is returned; pinned event is cleared. | Pass on disposable Synapse; Element rendering remains manual |
| Reaction feedback | Owner reaction reaches the agent but produces no visible bot answer. | Pass with fake transport on disposable Synapse |
| Restart/recovery | Accepted state and room mapping survive restart; active queue behavior is reported honestly. | Pass on disposable Synapse; report |
| Device SAS verification | Real Element device completes emoji comparison and `ja`. | Manual evidence; otherwise remains issue #1 |

## Unsupported or unverified claims

The following must not be presented as proven today:

- “Verified with Element” unless a real device SAS flow is recorded.
- “Cross-signed” or “automatically trusted on every device.”
- “Fully tested end to end” while coverage is unit tests plus mocks.
- “Any Codex conversation from any device” without checking device trust and
  the one-owner allowlist.
- “Voice works with any audio format or language at production quality.”
- “Messages are invisible to the homeserver.” E2EE protects content, while
  Matrix metadata remains visible to the homeserver.
- “Zero downtime” or “restart-safe” without the restart evidence above.

## Redaction checklist

Review every artifact before commit:

```bash
rg -n -i \
  'access[_ -]?token|password|secret|recovery[_ -]?key|private[_ -]?key|@[^ ]+:[^ ]+|![^ ]+:[^ ]+|mxc://[^ ]+' \
  docs/artifacts
```

The check should produce only intentional synthetic values such as
`@owner:matrix.example.org`. Inspect images manually because text search cannot
detect a room name drawn in a screenshot or a notification preview.

Do not retain raw `state.json`, Matrix crypto stores, user/device keys, access
tokens, password-manager output, private homeserver logs, or unredacted
timeline exports.

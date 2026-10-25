# Changelog

## 0.11.0

- Add encrypted arbitrary-file attachments in both directions, including
  markerless audio routing, private admission snapshots, original metadata,
  safe generated storage paths, and restart-safe outbound delivery.
- Edit each voice-note status message into the local transcript or an explicit
  terminal failure instead of posting a separate progress transcript.
- Preserve canonical Matrix thread roots for text, image, generic-file and
  voice prompts, including prompts that are already thread replies.
- Harden restart recovery for prompts, answers, attachments and OpenRig qitems;
  requests whose prompt-send effect became ambiguous fail closed instead of
  being replayed.
- Reject unsafe Matrix endpoints, unsafe agent path components and credential
  material in URLs.
- Redact activity-card content and bound commentary, terminal-card and
  delivery-failure recovery.
- Add accessible setup documentation, the Relay symbol and theme-safe avatar
  assets while retaining the OpenRig Matrix Bridge product name.
- Add deterministic documentation, link, package, clean-install and exact-source
  Matrix release gates.
- Add an operator runbook for candidate installation, single-instance checks,
  live attribution smoke tests, rollback and exact artifact provenance.

## 0.10.0

- Add default `auto`, explicit `steer`, and strict FIFO `queue` modes for
  short same-thread follow-ups.
- Batch every source event and qitem under the canonical thread, use transcript
  turn identity to reject unrelated human answers, and fall back losslessly when
  best-effort pane injection is unavailable or ambiguous.
- Treat an owner leaving or rejecting an agent room as a durable opt-out instead
  of recreating the room on restart.

## 0.9.0

- Pass Matrix reactions to the responsible agent as silent feedback without
  creating visible replies or inbound queue items.

## 0.8.1

- Restore the complete unique ghost avatar pool and the dedicated OpenRig
  profile avatar.

## 0.8.0

- Create priority-aware OpenRig queue items for accepted Matrix messages and
  track their claim and completion lifecycle.

## 0.7.1

- Refine the generated avatar silhouettes so each pool entry is visually
  distinct.

## 0.7.0

- Add automatic, persistent, collision-free avatar assignment for agent rooms
  and the bridge profile.

## 0.6.0

- Add 32 original OpenRig agent avatars with deterministic collision-free assignment.
- Preserve custom room avatars by default and provide explicit avatar replacement.

## 0.5.0

- Replace the Moshi gateway with direct OpenRig seat transport.
- Resolve Codex transcripts from the live OpenRig node inventory.
- Deliver Matrix prompts with `rig send --raw` and no separate chat gateway.
- Mirror new OpenRig human updates and decisions into the source agent's room.
- Register the Matrix owner as an OpenRig human recipient during `setup`.
- Mirror visible Codex commentary into one editable Matrix status message per request.
- Reduce active transcript and outbox polling latency to 200 ms.
- Poll OpenRig human-queue handoffs every two seconds by default.
- Show pending Matrix prompts in one editable pinned queue card.

## 0.4.0

- Render agent Markdown as native Matrix formatted messages, including tables.
- Receive encrypted PNG, JPEG, GIF, and WebP images into a per-agent inbox.
- Add `send-image` for encrypted image delivery from an agent to its room.
- Add `send-message` for explicit formatted agent messages into Matrix.

## 0.3.0

- Add pinned OpenRig decisions and encrypted multiple-choice polls.
- Return Matrix poll selections to the responsible agent as queued prompts.
- Name Matrix rooms after the agent role only; the bridge profile is `OpenRig`.

## 0.2.0

- Add per-agent Matrix live status through stable room topics and structured
  room state, without changing the room name or generated avatar.
- Show a persistent typing indicator while an agent is working.
- Add NVIDIA Parakeet TDT 0.6B v3 voice transcription with automatic model
  download and checksum verification.
- Add Matrix emoji SAS device verification.
- Add `setup`, `doctor`, and `download-model` commands.

## 0.1.0

- Initial OpenRig agent-to-Matrix bridge.

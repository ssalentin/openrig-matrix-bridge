# Changelog

## 0.5.0

- Replace the Moshi gateway with direct OpenRig seat transport.
- Resolve Codex transcripts from the live OpenRig node inventory.
- Deliver Matrix prompts with `rig send --raw` and no separate chat gateway.
- Mirror new OpenRig human updates and decisions into the source agent's room.
- Register the Matrix owner as an OpenRig human recipient during `setup`.
- Mirror visible Codex commentary messages into the active Matrix thread.

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

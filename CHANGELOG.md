# Changelog

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

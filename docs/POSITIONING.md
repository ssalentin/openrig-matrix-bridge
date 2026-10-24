# Product positioning and demo

This note is for Sebastian and any future launch review. Copy may be used
publicly only when the evidence row it relies on is marked verified in
[Verification and evidence](VERIFICATION.md).

## Positioning

**One sentence:** OpenRig Matrix Bridge gives each live OpenRig Codex seat its
own private, encrypted Matrix room, so work can continue from a phone or another
Matrix client without starting a second agent.

**Short description:** The bridge connects existing OpenRig Codex terminals to
private Encrypted Matrix rooms. Status, progress, images, voice transcription,
polls, reactions, and durable queue items stay attached to the same live seat.

**Who it is for:** A single OpenRig operator who wants a convenient,
multi-device Matrix interface without rebuilding the agent runtime around a new
chat gateway.

**What is deliberately not claimed:** Runtime portability beyond Codex,
production-scale multi-tenant use, complete Element cross-signing, or
comprehensive live-server coverage before the integration artifact exists.

## Evidence-backed value

These bullets are suitable for an internal release note. The public version
must wait for the live checks named in the verification matrix.

- **Same agent, different surface.** Matrix prompts are submitted to the
  resolved live Codex seat through `rig send --raw`; the bridge does not launch
  a duplicate agent. Unit evidence covers resolution and send-command
  construction.
- **State is visible where the conversation happens.** The room topic and a
  structured Matrix state event carry agent status; typing is refreshed during
  work. Status event construction is unit-tested; a live typing check remains
  pending.
- **Media and decisions stay in context.** Encrypted images, local voice
  transcription, pinned briefs, polls, reactions, and queue cards are
  represented in the same room. Protocol and storage paths are unit-tested;
  live encryption and client rendering require the integration evidence.
- **OpenRig remains the work ledger.** Matrix prompts become prioritized
  OpenRig qitems, are claimed when work starts, and close after answer delivery.
  The command sequence is unit-tested; the integration harness uses a fake
  OpenRig CLI.

## Conditional launch copy

Do not publish this section until the matching claim rows are verified.

> OpenRig Matrix Bridge gives every live OpenRig Codex agent a private,
> encrypted Matrix room. Continue the same conversation from Element, see
> whether the agent is working, send screenshots or voice notes, and make
> decisions in place—without running a second agent or moving the work out of
> OpenRig.

## Five-minute demo

Before recording, complete the manual setup and keep tokens, room IDs, MXIDs,
and private infrastructure out of frame.

1. Show the doctor result with the token already in the environment.
2. Open one agent room. Point out the stable room name, private/encrypted
   status, and idle topic.
3. Send a simple prompt. Show the typing indicator and `arbeitet` topic while
   it runs, then the final answer in the thread.
4. Send a second prompt while the first is active. Show the pinned queue card.
5. Send a screenshot and ask the agent to describe it.
6. Send a short voice note. Show the local transcript before the answer.
7. Show a decision poll and return the selected option to the agent.
8. React to a bridge message. Explain that the reaction is silent feedback.
9. Close on the durable qitem and the README setup path.

Every demo item should name the corresponding evidence row. Omit unsupported
items rather than recording a simulated success.

## Objection handling

### Is this another chat bot?

No. The bridge is a transport and UI adapter around an existing live OpenRig
seat. The agent process and its transcript are not duplicated.

### Is it end-to-end encrypted?

Newly created rooms request Matrix Megolm encryption, and media is encrypted.
Matrix homeserver metadata is still visible. Device verification and
cross-signing are separate manual limitations tracked in Issue #1.

### Does speech leave the machine?

After the model download, audio processing uses local `ffmpeg` and
`sherpa-onnx`. There is no transcription service API in the implementation.
Recognition quality still requires an observed voice sample.

### Can I use Claude or another runtime?

Not in the supported flow described here. The resolver and tests currently
target Codex.

## Post-evidence checklist

- Link the final README, evidence matrix, and redacted artifacts.
- Record the tested commit and ephemeral Matrix environment.
- Keep Issue #1 open unless real Element SAS verification is observed.
- Remove any “fully verified” language that the evidence table does not
  support.
- Do not announce or publish without the owner's separate authorization.

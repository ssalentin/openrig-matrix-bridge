# Product name decision brief

Status: decision required. This document does not rename the repository,
package, command, imports, configuration, or release artifacts.

## Recommendation

**Keep `OpenRig Matrix Bridge` for 0.11.0. Defer a public rename.**

The current name is long but immediately tells a technical evaluator what the
product does and which two systems it joins. It is already connected to the
repository, package, command, docs, release history, and service files. The
strongest short alternative, `RoomRig`, is understandable only after learning
OpenRig's "rig" vocabulary and has existing uses in room-scale calibration and
an AV-adjacent company. A rename immediately before release would add migration
risk without a demonstrated discoverability gain.

The proposed symbol identities are name-independent. The owner can select a
symbol now and revisit naming separately without blocking 0.11.0.

## Current name assessment

| Dimension | Assessment |
|---|---|
| Discoverability | Strong for searches combining OpenRig, Matrix, and bridge; weak as a casual brand. |
| Memorability | Three clear tech words, but long and generic in the middle. |
| Pronunciation | Unambiguous once OpenRig is known. |
| Package/repo ergonomics | `openrig_matrix_bridge`, `openrig-matrix-bridge`, and the matching CLI are already established. |
| First-time comprehension | High for the intended technical evaluator: existing OpenRig agents, Matrix as UI, bridge as transport. |
| Main weakness | It is a category description rather than a distinctive product name. |

## Shortlist

Scores are 1 (weak) to 5 (strong). For migration cost, 5 means lowest cost.

| Name | Story | Clarity | Fit | Distinctiveness | Professionalism | Migration cost | Future scope |
|---|---|---:|---:|---:|---:|---:|---:|
| OpenRig Matrix Bridge | The bridge between OpenRig and Matrix. | 5 | 5 | 2 | 5 | 5 | 2 |
| RoomRig | A private room for each OpenRig seat. | 3 | 5 | 3 | 4 | 2 | 4 |
| SeatRoom | Every agent seat has its own room. | 4 | 4 | 2 | 4 | 2 | 3 |
| Seatline | A direct conversation line to each agent seat. | 4 | 4 | 3 | 4 | 2 | 4 |
| RoomRelay | Messages relay between owner and agent inside the room. | 3 | 3 | 2 | 4 | 2 | 4 |

`RoomRig` is the strongest shorter option, but not yet a release-safe rename:
it relies on OpenRig vocabulary, already appears in unrelated room-calibration
software, and has a same-name legal entity in search results.

`SeatRoom` and `Seatline` are clearer in isolation but collide with active salon,
cinema, childcare, and outsourcing uses. `RoomRelay` is generic and its exact
GitHub handle is occupied.

`RigRoom` was rejected during evaluation: an exact Linux guitar-effects product
and a crypto mining game using `rigrooms.com` make it materially more crowded.
`MatrixRig` was also rejected because it reads as linear algebra, AV matrix
routing, or simulation hardware.

## Availability probes

Checked from this environment on 2026-10-24 between 18:26Z and 18:31Z.
Availability is a point-in-time signal, not reservation or legal clearance.

| Name | PyPI exact | GitHub owner repo | GitHub global user/org | npm exact | `.com` DNS |
|---|---:|---:|---:|---:|---:|
| roomrig | 404 | 404 | 404 | 404 | resolves; owner unknown |
| seatroom | 404 | 404 | 404 | 404 | resolves; owner unknown |
| seatline | 404 | 404 | 404 | 404 | resolves; owner unknown |
| roomrelay | 404 | 404 | 200 occupied | 404 | resolves; owner unknown |
| matrixrig | 404 | 404 | 404 | 404 | no response |
| rigroom | 404 | 404 | 404 | 404 | resolves; owner unknown |

The current package also returns 404 from the public PyPI API because no PyPI
release exists. A 404 therefore does not prove that any candidate is available
for publication or that a domain can be acquired.

Obvious exact-name search collisions found:

- `RoomRig`: a Unity room-calibration object and `Roomrig Nigeria Ltd`.
- `SeatRoom`: an exact salon-software product listing.
- `Seatline`: cinema-seat software, an outsourcing service, and childcare
  software uses.
- `RoomRelay`: the exact GitHub user/org handle is occupied; a web-product
  collision search was inconclusive.
- `RigRoom`: exact Linux audio software and a crypto game.
- `MatrixRig`: energy/matrix-rig technical uses and simulation-rig associations.

No trademark search, company-registry search, app-store search, or domain
ownership verification was performed.

## Why keep the current name

1. The intended audience already knows OpenRig and Matrix; clarity is more
   valuable than a clever metaphor.
2. Every shortlist alternative requires explanation and has a collision or a
   less precise scope.
3. A release rename changes install and operational identity, while 0.11.0's
   product work is already substantial.
4. The current name accurately avoids claiming broader runtimes or federation.
5. A better name can be selected with owner research after this release; the
   symbol proposal can proceed independently.

## Migration impact if the owner later approves a rename

- **Repository URL:** rename the GitHub repository and rely on GitHub redirects
  temporarily; update clone/setup examples, issue/release links, topics, package
  metadata, badges, and docs.
- **Python distribution:** publish the new distribution name. Keep
  `openrig_matrix_bridge` as an import compatibility package so existing code
  and scripts do not need immediate changes; deprecate it across at least one
  release.
- **Console command:** add the new command as the primary entry point while
  retaining `openrig-matrix-bridge` as a deprecated alias. Update the systemd
  launcher only after the alias is proven.
- **Imports:** do not rename the module at the same time as the distribution;
  a shim package should re-export old imports and emit a deprecation warning.
- **Configuration:** preserve the current default config path, environment
  variables, service paths, state directory, Matrix store, and inbox/outbox
  locations. Otherwise existing rooms, E2EE keys, pending work, and attachments
  become orphaned.
- **Docs and claims:** update title, positioning, verification, setup, support
  links, service examples, release notes, artifact paths, and screenshots.
- **Release artifacts:** wheel/sdist names and checksums change. Publish aliases
  in a deliberate version, then remove old package/CLI aliases only in a later
  breaking release.
- **OpenRig integration:** keep registered human destinations, bridge sender
  identity, and queue source addresses stable unless explicitly migrated.
- **State compatibility:** test upgrade from the current name with existing
  encrypted state before changing any default path.

## Decision request

Sebastian and the Engineering Lead should choose one:

1. **Recommended:** keep `OpenRig Matrix Bridge` for 0.11.0 and defer naming.
2. Approve `RoomRig` as a future rename and schedule separate compatibility work.
3. Request a different shortlist after targeted owner/domain/trademark research.

No public rename occurs without explicit approval.

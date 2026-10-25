# Relay identity

## Selected identity: Relay

Relay is the selected symbol for the 0.11.0 OpenRig Matrix Bridge identity.
Two interlocking conversation forms share one signal node: the bridge keeps the
person and the existing OpenRig seat in one continuous exchange. The product
name remains **OpenRig Matrix Bridge**.

The earlier proposal alternatives are not shipped. Relay is the single product
mark used by the README, Matrix profile, release assets, and usage guidance.

## Asset set

The source of truth is the checked-in SVG. The PNG exports are deterministic
derivatives for Matrix and documentation use.

| Use | 256px | 32px | Source |
|---|---|---|---|
| Light background | [PNG](../../src/openrig_matrix_bridge/assets/branding/export/relay-secondary-light-256.png) | [PNG](../../src/openrig_matrix_bridge/assets/branding/export/relay-secondary-light-32.png) | [SVG](../../src/openrig_matrix_bridge/assets/branding/source/relay-secondary-light.svg) |
| Dark background | [PNG](../../src/openrig_matrix_bridge/assets/branding/export/relay-secondary-dark-256.png) | [PNG](../../src/openrig_matrix_bridge/assets/branding/export/relay-secondary-dark-32.png) | [SVG](../../src/openrig_matrix_bridge/assets/branding/source/relay-secondary-dark.svg) |
| Matrix profile | [PNG](../../src/openrig_matrix_bridge/assets/branding/export/relay-avatar-256.png) | [PNG](../../src/openrig_matrix_bridge/assets/branding/export/relay-avatar-32.png) | [SVG](../../src/openrig_matrix_bridge/assets/branding/source/relay-avatar.svg) |

The Matrix profile uses the contained dark-tile avatar. One profile image cannot
follow the viewer's light or dark theme, so the contained variant preserves the
mark's contrast in both clients. The transparent light and dark exports remain
the design masters for documentation and future surfaces.

## Preview

| Matrix profile on light | Matrix profile on dark |
|---|---|
| ![Relay profile on light](artifacts/relay-avatar-light-bg-preview.png) | ![Relay profile on dark](artifacts/relay-avatar-dark-bg-preview.png) |

## Usage

- Keep clear space around the symbol equal to at least one quarter of its width.
- Use the light-background SVG on light surfaces and the dark-background SVG on
  dark surfaces.
- Keep the transparent background; do not redraw or recolor individual paths.
- Use the contained avatar for the bot's Matrix profile. Do not upload one of
  the transparent theme-specific masters as a profile image.
- The standalone symbol is the complete 0.11.0 identity. Do not add text inside
  the mark or create a separate wordmark without a new brand review.

The bridge assigns the existing 32-image pool to individual agent rooms. The
bot profile itself uses `relay-avatar-256.png` through the runtime asset loader.

## README

The project README embeds the local light/dark PNG pair with a responsive,
compact picture. It uses no external image host, fixed markup loads at 72x72
pixels, and the accessible name is `Relay`. Longer identity, preview, usage, and
licensing guidance stays on this page rather than in the landing page.

## Licensing

All Relay source and export files were authored for this repository and are
released under the repository's [MIT license](../../LICENSE). They contain no
embedded third-party artwork, fonts, or trademarks. External visual similarity
and trademark clearance are not claimed.

---
version: beta
name: gops-coinbase-glass-market-workspace
description: "Current GOPS website design system for the implemented market workspace: Coinbase trust colors, a white-to-blue glass app canvas, S&P 500 treemap home, tiled chart panels, a left company rail, and a fixed bottom agent command bar."

sourceOfTruth:
  tokens: "apps/gops-frontend/src/styles.css"
  frontendThemeBridge: "apps/gops-frontend/src/theme/colors.ts"
  chartFallbackTheme: "apps/chart-engine/src/theme.ts"
  treemapToneLogic: "apps/gops-frontend/src/treemap/treemapColors.ts"

colors:
  brand-blue: "#0052ff"
  brand-blue-active: "#003ecc"
  brand-blue-soft: "#e7efff"
  brand-blue-disabled: "#b9c0ca"
  ink: "#0a0b0d"
  ink-soft: "#242832"
  body: "#5b616e"
  muted: "#8c939f"
  muted-soft: "#b9c0ca"
  canvas: "#ffffff"
  canvas-oklch: "oklch(100% 0 0)"
  canvas-mid: "#fbfdff"
  canvas-mid-oklch: "oklch(99.6% 0.002 247)"
  canvas-end: "#eff6ff"
  canvas-end-oklch: "oklch(98.4% 0.010 250)"
  panel-glass: "rgba(255, 255, 255, 0.05)"
  panel-glass-strong: "rgba(255, 255, 255, 0.08)"
  glass-border: "rgba(255, 255, 255, 0.30)"
  glass-edge: "rgba(255, 255, 255, 0.80)"
  glass-edge-soft: "rgba(255, 255, 255, 0.30)"
  hairline: "rgba(10, 11, 13, 0.08)"
  hairline-strong: "rgba(10, 11, 13, 0.14)"
  button-surface: "transparent / same as background"
  button-pressed-shadow: "inset 7px 7px 7px rgba(184, 198, 216, 0.50), inset -7px -7px 7px rgba(255, 255, 255, 0.92)"
  positive: "#05b169"
  positive-glow: "#6cff5f"
  negative: "#cf202f"
  negative-glow: "#ff5f7a"
  warning: "#f4b000"
  warning-glow: "#ffe45f"
  on-primary: "#ffffff"
  on-dark: "#ffffff"

typography:
  ui:
    fontFamily: "'Coinbase Sans', Inter, Arial, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    baseSize: "14px"
    letterSpacing: 0
  compact-label:
    fontSize: "10px-13px"
    fontWeight: "700-820"
    lineHeight: 1
    letterSpacing: 0
  data-control:
    fontSize: "11px-12px"
    fontWeight: "700-760"
    lineHeight: 1
    letterSpacing: 0
  body:
    fontSize: "12px-14px"
    fontWeight: 400
    lineHeight: "1.4-1.56"
    letterSpacing: 0

layout:
  gutter: "24px"
  top-nav-height: "48px"
  workspace-top-inset: "52px"
  bottom-nav-height: "64px"
  bottom-control-size: "36px"
  surface-radius: "16px"
  app-glass-radius: "20px"
  side-overlay-radius: "28px"
  heatmap-cell-radius: "16px"
  heatmap-band-radius: "7px"
  chart-candle-radius: "3px"
  panel-padding: "8px"
  panel-gap: "12px"
  mobile-breakpoint: "640px"

effects:
  app-canvas: "White canvas with a very low blue horizon glow: layered bottom-centered radial gradients over #ffffff -> #fbfdff -> #eff6ff."
  glass-surface:
    background: "top white edge gradient + left white edge gradient + panel glass"
    border: "1px solid rgba(255, 255, 255, 0.30)"
    backdropFilter: "blur(10px)"
    boxShadow: "7px 7px 7px rgba(184, 198, 216, 0.48), inset 0 1px 0 rgba(255, 255, 255, 0.50), inset 0 -1px 0 rgba(255, 255, 255, 0.10), inset 0 0 4px 2px rgba(255, 255, 255, 0.20)"
  strong-glass:
    background: "same edge treatment over rgba(255, 255, 255, 0.08)"
  bottom-command-band:
    background: "transparent-to-white vertical gradient with blur(18px); chrome inside the band remains mostly transparent."
  command-buttons:
    default: "No visible fill or raised shadow; button blends into the background."
    active: "Same background with inset 7px paired shadows, creating a pressed control state."
---

## Overview

GOPS is a dense market workspace, not a landing page and not a generic personal finance dashboard. The first viewport is the product: an S&P 500 treemap home or a tiled analysis workspace with charts, market panels, an agent command input, and company-specific side actions.

The visual direction is Coinbase-inspired trust UI: white canvas, crisp black ink, blue as the primary action color, green/red/yellow only for financial state, and very light glass surfaces. The implementation is deliberately quieter than a trading terminal. Panels are translucent and low contrast, while market data, chart marks, and active controls carry the emphasis.

## Design Principles

- Keep the app operational and scan-dense. Avoid marketing hero structure, oversized editorial headings, and explanatory in-app copy.
- Treat the canvas as a full-viewport workspace. Navigation, commands, panels, and docks float over the same market surface.
- Use glass sparingly but consistently: panel borders are white glass edges, not dark card outlines.
- Use blue for primary actions, selected submits, active tabs, and focused interaction states.
- Use green, red, and yellow only for market movement, alert severity, chart indicators, and financial status.
- Keep text compact. Panel names are secondary chrome and should appear only on hover or in layout-edit mode.

## Token Contract

The active tokens live in `apps/gops-frontend/src/styles.css` under `:root`. New components should consume the existing `--coinbase-*`, `--color-*`, and `--gops-*` variables instead of introducing local palettes.

Important aliases:

- `--coinbase-primary`: primary blue.
- `--coinbase-ink`: default text and chart ink.
- `--coinbase-body`, `--coinbase-muted`, `--coinbase-muted-soft`: text hierarchy.
- `--coinbase-panel-glass`, `--coinbase-panel-glass-strong`, `--coinbase-rail-glass`: transparent surface fills.
- `--coinbase-glass-border`, `--coinbase-glass-edge`, `--coinbase-glass-shadow`: shared glass treatment.
- `--color-up`, `--color-down`, `--color-caution`: semantic finance colors.
- `--layout-gutter`, `--bottom-nav-height`, `--bottom-control-size`, `--surface-radius`: shared sizing rhythm.
- `--heatmap-cell-radius`, `--heatmap-band-radius`, `--chart-candle-radius`: market-shape radius values, visually derived from the white canvas and `--surface-radius`.

When adding a new theme token, also update the theme bridge in `apps/gops-frontend/src/theme/colors.ts` if chart, treemap, ontology, or canvas code needs to read it.

## App Canvas

The page floor is `--coinbase-app-canvas`: a white background with a blue atmospheric horizon concentrated near the bottom of the viewport. It uses layered radial gradients plus a white-to-blue vertical gradient. Keep the top area mostly white so chart panels and treemap labels stay readable.

`app-shell` owns the full viewport and isolates layers. A fixed `heatmap-background-layer` adds subtle bottom-depth effects; its treemap child is currently hidden for the decorative layer, while the visible home treemap is rendered in the workspace.

Avoid:

- Pure white body without the blue horizon glow.
- Dark hero sections.
- Purple or multi-hue marketing gradients.
- Decorative blobs or unrelated illustrations.

## Primary Screens

### Treemap Home

The home view is a full workspace `TreeMapCanvas` for the S&P 500 universe. Tiles use area for market weight and green/red/muted ink for movement. Change intensity is encoded by opacity, not by switching to a saturated background for every tile.

Treemap panel treatment:

- Absolute positioned inside the workspace.
- `16px` radius by default.
- Symbol cells use `--heatmap-cell-radius`; industry bands use `--heatmap-band-radius`, clamped by tile size so small tiles stay continuous rather than pill-shaped.
- Glass border and edge highlights.
- Transparent canvas.
- Hover metadata sits as a compact glass strip near the bottom-left of the symbol tile column.

### Chart Workspace

The chart workspace is a tiled grid managed by `PanelWorkspace`. Panels are absolute workspace surfaces, not page sections. The layout can be resized, edited, saved as presets, and populated with chart, company, news, indices, recommendation, portfolio, order, and ontology panels.

Workspace panels:

- Use `workspace-panel-surface` glass.
- Use `--surface-radius` unless chart lanes join across internal boundaries.
- Avoid permanent title bars. `workspace-panel-nav` is a small hover overlay only.
- Keep panel content inside the `--panel-padding` and `--panel-gap` contract unless the panel is intentionally full-bleed, such as a chart canvas.

Chart lanes can remove internal radius so adjacent chart panels read as one analysis strip. Boundary hover and active states should remain subtle, using ink opacity rather than heavy resize handles.

## Navigation And Commands

### Bottom Command Bar

`workspace-bottom-nav` is fixed at the bottom and is not part of the customizable panel grid. It uses a transparent-to-white blurred band. The central `agent-box` is the main command surface and should stay visually lighter than a card.

Command bar behavior:

- Center column holds the agent input, fluid from about `280px` to `560px`.
- Side action buttons are icon-first `36px` circles.
- Base buttons inside the bottom band are transparent; active and hover states turn blue.
- The stop action uses semantic red.
- The chat panel slides up from the bottom center and stays wider than the input.

### Left Company Rail

`index-side-rail` is a compact pill rail pinned to the left side around mid-viewport. It is partially tucked off-canvas until hover/focus, then slides into view. It contains the selected company logo and side actions for alerts, watchlist/news, and settings/account flows.

Rail behavior:

- Company logo button is the top active item.
- Action buttons are circular glass controls.
- Active side action uses dark ink fill with white icon.
- The rail stays visible on touch devices because hover is unavailable.

### Side Overlay Menus

Side menus open from the left rail as `bottom-menu-panel side-overlay`. They use stronger glass, `28px` radius on desktop, and sit left of the content without replacing the workspace. On mobile, they shrink between the rail and the right viewport edge.

## Panels And Surfaces

The default surface recipe is:

```css
border: 1px solid var(--coinbase-glass-border);
border-radius: 20px;
background:
  linear-gradient(90deg, transparent, var(--coinbase-glass-edge), transparent) top / 100% 1px no-repeat,
  linear-gradient(180deg, var(--coinbase-glass-edge), transparent, var(--coinbase-glass-edge-soft)) left / 1px 100% no-repeat,
  var(--coinbase-panel-glass);
box-shadow: var(--coinbase-glass-shadow);
backdrop-filter: var(--coinbase-glass-filter);
```

Use this treatment for workspace panels, menu panels, chat panels, alerts, symbol search menus, ontology panels, and market cards. For repeated rows inside a panel, use lighter `--coinbase-surface-card` or `--coinbase-surface-soft` states instead of nesting another heavy card.

Scrollbar styling is part of the visual identity. Panel scrollbars use a thin black stem on the right or bottom edge with the thumb mostly buried in the rule. Do not replace this with wide browser-default scrollbars in panel content.

## Chart UI

Charts inherit colors through `readThemeColors()` and `chartDocumentStyleFromTheme()`. The chart engine fallback theme mirrors the CSS tokens, so chart rendering remains consistent before CSS sync completes.

Chart conventions:

- Canvas and chart wrappers are transparent over the glass panel.
- The chart topbar is an overlay, not a permanent toolbar block.
- Symbol search, page-sync, interval, and chart-type controls reveal on chart hover or focus.
- Select controls start visually quiet and become bordered only when interactive.
- MA colors are blue, green, and yellow. Volume and grid use low-opacity ink.
- Drawings, crosshair, and preview marks use ink unless a specific layer color is required.
- Candle bodies use a compact rounded rectangle treatment matching `--chart-candle-radius`, with rounded wicks, so chart marks share the same softened market-shape language as the treemap.

## Tool Docks And Presets

Layout presets, drawing tools, and add-layer tools sit above the bottom command band. They are fixed docks, not panel content.

Preset dock:

- Centered above the command bar.
- Pill buttons with light raised surfaces.
- Active or hovered preset changes text weight, not fill.
- Save, delete, and edit are icon buttons.

Drawing/add docks:

- Compact icon controls.
- Active drawing/add targets use primary blue.
- Layer buttons may use their layer accent color when active.

## Controls

Buttons and controls are compact and icon-first where possible.

Default control:

- `36px` minimum touch/control size for nav buttons.
- `4px` radius for many form-like controls after the Coinbase application layer.
- Transparent or glass background by default.
- Blue border/fill for primary active states.

Input controls:

- White canvas fill.
- `4px` radius.
- Hairline border.
- Ink border on focus.

Semantic states:

- Positive: `#05b169`.
- Negative/danger: `#cf202f`.
- Warning/caution: `#f4b000`.
- Do not use semantic colors as broad page backgrounds.

## Content Panels

Market, news, recommendation, company, portfolio, order, and ontology panels should follow the same density:

- Small headings, usually `12px-13px`.
- Strong labels at medium weight rather than oversized bold.
- Rows with compact vertical rhythm.
- Ellipsis for long symbols, company names, and source labels.
- Data status, empty, and error rows use soft glass and muted text.
- Actions remain local and icon-led when the action is obvious.

Chart and treemap content can be full-bleed. Text-heavy panels should preserve `--panel-padding` and avoid adding another outer card inside the workspace panel.

## Responsive Rules

At `max-width: 640px`:

- Bottom nav becomes an agent column plus compact right actions.
- The right action group stays near the command bar and can scroll horizontally.
- Side overlays use `left: 54px` and `right: 8px`.
- The side rail remains visible.
- Preset dock is centered above the command band with `calc(100vw - 16px)` max width.

Maintain stable dimensions for panels, controls, and docks so hover, loading, and selected states do not shift layout.

## Do

- Build on the existing root CSS tokens.
- Keep the first screen as the usable market workspace.
- Use glass edge highlights and subtle blur consistently.
- Let charts, treemap tiles, and market rows carry the color intensity.
- Use lucide icons for command and navigation controls.
- Keep all letter spacing at `0`.
- Keep panel title chrome hidden until hover/edit mode.

## Do Not

- Reintroduce the old personal-finance/credit-score dashboard concept into GOPS.
- Add a landing-page hero or marketing card stack.
- Make every panel opaque white.
- Use saturated blue as the full app background.
- Use green/red/yellow outside financial state.
- Add nested cards inside cards for basic layout.
- Add duplicate local palettes that bypass `styles.css` tokens.

## Implementation Checklist

Before merging a design change:

1. Confirm new colors are expressed as root tokens or existing semantic aliases.
2. Confirm chart/treemap/ontology rendering still reads from the theme bridge when needed.
3. Check desktop and mobile widths around the `640px` breakpoint.
4. Verify bottom command controls, side rail menus, and tool docks do not overlap.
5. Keep `DESIGN.md`, `styles.css`, and theme bridge notes aligned when the visual contract changes.

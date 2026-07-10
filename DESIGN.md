---
version: alpha-current
name: GOPS workspace design system
description: "A dark, dense market-analysis workspace built around a full-screen heatmap or chart canvas, compact glass panels, and a bottom agent command bar. The visual priority is repeat trading/research work: scan fast, compare panels, keep chrome quiet, and reserve accent color for action, focus, and live market state."
source:
  app: apps/gops-frontend
  styles: apps/gops-frontend/src/styles.css
  entry: apps/gops-frontend/index.html
updatedFor:
  font: Asta Sans
  typeScale: 5 semantic sizes
  chrome: side rail removed
  build: split production chunks
---

# GOPS Design

This file describes the current GOPS frontend, not the older Framer-inspired
marketing reference. Use it when changing `apps/gops-frontend` UI, layout,
or visual tokens.

## Product Shape

GOPS is a full-screen financial workspace. The first screen is the product
itself: a market treemap or a tiled chart workspace with an always-available
agent command input. It is not a landing page, documentation page, or marketing
site.

Primary modes:

- `treemap`: S&P 500 market map as the central canvas.
- `chart`: resizable panel workspace containing charts, news, order flow,
  comparisons, company data, ontology, recommendations, and portfolio panels.
- `agent`: bottom command input plus optional chat log, layered over the
  workspace without becoming a side panel.

The side rail and side overlay menu have been removed. Do not reintroduce
side buttons, side panels, or hidden edge rails unless the product direction
explicitly changes.

## Design Principles

- Keep the canvas first. Panels and chrome should feel attached to the data,
  not like a separate dashboard frame.
- Favor dense but calm information surfaces. This is an operational tool for
  repeated scanning and comparison.
- Use low-contrast glass for containers, white/gray text for hierarchy, and
  one blue action color for selection and focus.
- Avoid marketing composition: no hero sections, no oversized editorial copy,
  no decorative blobs, no promo cards.
- Use icon controls for tools and compact commands. Text buttons are reserved
  for explicit labels such as `Leave`.
- Do not add a side rail. Navigation belongs in the top preset dock, the panel
  workspace, or the bottom agent command bar.

## Tokens

The active tokens live in `apps/gops-frontend/src/styles.css`.

```yaml
colors:
  canvas: "#090909"
  canvas-right: "#0d0d0d"
  surface-1: "#141414"
  surface-2: "#1c1c1c"
  panel: "rgb(20 20 20 / 0.78)"
  panel-strong: "rgb(28 28 28 / 0.86)"
  control-glass: "rgb(28 28 28 / 0.84)"
  ink: "#ffffff"
  ink-soft: "#f5f5f5"
  ink-muted: "#999999"
  muted-soft: "#6f6f6f"
  hairline: "#262626"
  hairline-strong: "#333333"
  accent-blue: "#0099ff"
  accent-blue-active: "#33adff"
  up: "#22c55e"
  down: "#ff5577"
  caution: "#ff7a3d"

layout:
  top-nav-height: 48px
  bottom-nav-height: 64px
  control-size: 36px
  app-ui-scale: 1.6
  grid-gutter: "clamped 6px to 10px, based on viewport width"
  workspace-top-inset: 52px
  workspace-bottom-inset: 64px

radius:
  app-glass: 8px
  panel: 8px
  small-overlay: 6px
  chat-message: 14px
  pill: 999px

panel:
  padding: 8px
  gap: 12px
  border: "rgb(255 255 255 / 0.022)"
  shadow: "0 8px 26px rgb(0 0 0 / 0.064), inset 0 0 0 1px rgb(255 255 255 / 0.010)"
  filter: "blur(3px) saturate(107%)"
  note: "Glass intensity is reduced 60% from the previous stronger treatment."

typography:
  micro: 10px
  compact: 12px
  body: 14px
  title: 18px
  display: 32px
```

## Typography

The app loads Asta Sans from Google Fonts in `apps/gops-frontend/index.html`.
All primary font variables resolve to Asta Sans:

```css
--font-ui-serif: "Asta Sans", Arial, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
--font-ui-sans: "Asta Sans", Arial, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
--font-data-sans: "Asta Sans", Arial, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
```

The entire website uses exactly five font sizes. Treat them as semantic tokens,
not as a range from which to choose arbitrary intermediate values:

| Token | Size | Weight | Use |
| --- | ---: | ---: | --- |
| `--type-micro` | 10px | 600-800 | Chart ticks, timestamps, tertiary metadata, badges, and legal/attribution text |
| `--type-compact` | 12px | 500-800 | Buttons, inputs, tabs, table cells, panel labels, symbols, and secondary values |
| `--type-body` | 14px | 400-700 | Default workspace text, chat content, descriptions, and primary row values |
| `--type-title` | 18px | 600-800 | Panel headings, card headings, important prices, and section totals |
| `--type-display` | 32px | 600-800 | One dominant portfolio value, score, or empty-state number within a view |

CSS token contract:

```css
--type-micro: 10px;
--type-compact: 12px;
--type-body: 14px;
--type-title: 18px;
--type-display: 32px;
```

Canvas and SVG renderers use the matching numeric constants from
`apps/gops-frontend/src/theme/typography.ts`.

Component mapping:

| Component content | Token |
| --- | --- |
| Chart axes, tiny metadata, status badges | `--type-micro` |
| Top actions, agent input, panel chrome, controls | `--type-compact` |
| Chat messages, news summaries, default copy | `--type-body` |
| Panel headings and emphasized market values | `--type-title` |
| Primary account/portfolio metric only | `--type-display` |

Rules:

- Use Asta Sans everywhere unless a canvas renderer needs its own numeric
  drawing metrics.
- Do not declare literal `font-size` values inside components. Use one of the
  five tokens above, including for canvas text constants where practical.
- Do not use intermediate sizes such as 11px, 13px, 15px, or 17px. Resolve
  hierarchy with weight, color, and spacing before moving up to another token.
- Each surface should normally use no more than three sizes: one default, one
  supporting size, and one emphasis size.
- Reserve `--type-display` for a single dominant metric. Operational panels
  should usually top out at `--type-title`.
- Keep letter spacing at `0` unless an existing component already has a
  specific technical reason.
- Keep headings compact inside panels. Do not use hero-scale type inside the
  workspace.
- Prefer tabular, legible numeric treatment in market-data rows and chart
  readouts.

## Layout

### App Shell

The app is a fixed full-viewport shell:

- `body`, `html`, and `#root` are `100vw`/`100vh` and `overflow: hidden`.
- `canvas-workspace` fills the viewport.
- A heatmap background layer can sit behind the active workspace.
- Top and bottom nav are fixed overlays with pointer events only on controls.

### Workspace Grid

Chart mode uses a tiled panel workspace:

- 4 logical grid columns.
- grid gutter: `round(min(10, max(6, viewportWidth * 0.006)))`.
- top inset: 52px.
- bottom inset: 64px.
- panels can be resized, moved, replaced, or removed in layout edit mode.

On narrow screens, the layout locks at or below 760px.

### Removed Side Surface

The old `index-side-rail` and `bottom-menu-panel.side-overlay` surfaces are no
longer part of the product. The current design has:

- no left side rail,
- no side menu buttons,
- no side overlay menu,
- no side watchlist/settings/portfolio drawer.

If a feature needs a home, use one of these surfaces instead:

- a workspace panel,
- the top preset dock,
- the bottom agent command,
- an in-panel popover,
- an alert toast.

## Surfaces

### Glass Panels

Most app panels share the same glass language:

```css
border: 1px solid var(--app-panel-glass-border);
border-radius: var(--app-glass-radius);
background: var(--app-panel-glass-background);
box-shadow: var(--app-panel-glass-shadow);
backdrop-filter: var(--app-panel-glass-filter);
```

The glass background is intentionally subtle: two faint diagonal edge
gradients over near-transparent black. It should frame data without turning
every panel into a decorative card.

Applies to:

- workspace panels,
- chart compare panels,
- news and company panels,
- order ticket,
- ontology panel,
- recommendations,
- alert toast,
- symbol search menu,
- bottom chat panel.

### Panel Chrome

Panel chrome is intentionally hidden by default:

- panel title appears as a small overlay only on hover or in layout edit mode;
- body content fills the panel;
- scrollbars are thin, black/white, and edge-aligned;
- chart panel controls stay inside the chart panel, not in global chrome.

### Treemap

The treemap is a full visual surface, not a card gallery:

- panel radius: 8px;
- background: transparent canvas with glass edge treatment;
- hover metadata appears top-left, compact and non-interactive;
- cell/tile radius is currently `0px` for heatmap cell geometry;
- up/down color uses semantic green/red, not decorative palette variants.

### Chart Workspace

Charts sit inside transparent panel frames:

- chart canvas background remains transparent over the dark app canvas;
- current price, comparison legends, drawing tools, and add-layer tools are
  overlays inside the chart panel;
- chart toolbar controls use icons and compact separators;
- chart drawing and add docks float just above the bottom command band.

### Index Panel

The `지수` panel is intentionally less shiny than the general panel system:

- no backdrop blur on `market-indices-panel` or `index-widget-panel`;
- border alpha is about half of the reduced shared glass border;
- shell shadow is minimal;
- rotating index cards override `surface-raised` with a flat dark fill;
- sparkline area fill stays low opacity so the chart does not glow.

Do not apply the full shared glass treatment to the index panel. It should read
like quiet market data, not a highlighted promotional tile.

## Navigation And Commands

### Top Nav

The top nav is quiet:

- fixed at top, 48px tall;
- transparent background;
- center slot is reserved for the preset dock in chart mode;
- right side contains only direct login/logout state.

There is no top alert/settings drawer trigger after the side-panel removal.

### Bottom Agent Command

The bottom command bar is the primary global command surface:

- fixed at bottom, 64px tall;
- transparent nav wrapper;
- centered `agent-box` spans the available width;
- pill radius (`999px`);
- dark translucent fill: `rgb(0 0 0 / 0.36)` plus faint top/bottom edge light;
- blur: `blur(10px) saturate(118%)`;
- contains reference chips, input, send/stop button, and chat toggle.

The agent input placeholder changes by mode:

- chart mode: `Agent에게 물어보기`
- treemap mode: `기업명/티커로 차트 열기`
- unauthenticated when auth is required: `로그인 후 Agent를 사용할 수 있습니다`

### Chat Panel

The chat panel opens above the agent box:

- same glass family as other panels;
- assistant/system messages sit in 14px rounded translucent message boxes;
- confidence is shown as a small tone dot when available;
- details/citations are collapsible to keep the command surface compact.

## Interaction States

Use color, not size, for most interaction feedback:

- hover/focus action color: `#0099ff`;
- active action color: `#0099ff`;
- stop/destructive color: `#ff5577`;
- positive market state: `#22c55e`;
- warning/caution/reference highlight: `#ff7a3d`;
- disabled opacity: about `0.5` to `0.58`.

Avoid layout shift on hover. Controls should not resize when hovered, focused,
or active.

## Component Rules

### Buttons

- Prefer icon buttons for chart tools, agent send/chat, drawing tools, and
  panel edit controls.
- Keep command buttons visually transparent by default.
- Use text only when the action is clearer as text, for example `Leave`.
- Minimum target should remain practical even when the visual icon is compact.

### Panels

- Use panels for working content, not for marketing copy.
- Do not put cards inside decorative cards.
- Use `--panel-padding: 8px` and `--panel-gap: 12px` unless a dense data view
  has an established local exception.
- Panel hover chrome should reveal controls without occluding essential data.

### Search And Inputs

- Inputs are borderless inside a pill or glass container.
- Symbol search menus use the glass surface and stay local to the invoking
  panel/control.
- Do not introduce a global search drawer.

### Alerts

- Alert toasts may appear globally.
- The removed side alert menu should not be restored as a side overlay.
- Toast actions may open a chart directly when a symbol is present.

## Responsive Behavior

Mobile and narrow layouts:

- bottom nav becomes a single-column command strip;
- agent box fills available width;
- agent input remains `--type-compact` (12px);
- side rail remains absent on all breakpoints;
- panel text must stay inside its bounds.

Do not scale fonts continuously with viewport or container width. Responsive
components may step down to the next smaller token, but must still use one of
the same five sizes. Prefer layout constraints and wrapping before reducing
type size.

## Build And Performance

Production build uses Rollup manual chunks in
`apps/gops-frontend/vite.config.ts`:

- `vendor-react`
- `vendor-d3`
- `vendor-icons`
- `chart-engine`
- `chart-core`
- `layout-core`
- `market-ui`
- `workspace-ui`
- `panel-ui`
- `vendor`

Do not fix chunk warnings by only raising `chunkSizeWarningLimit`. Prefer
stable chunk boundaries that match actual app domains.

## Do And Do Not

Do:

- keep the app full-screen and data-first;
- keep Asta Sans as the shared UI/data font;
- use dark glass panels with 8px radius and the reduced 40%-strength glass tokens;
- use the bottom agent command as the primary global command surface;
- keep chart and layout tools close to the panel they affect;
- verify `npm run build` after UI structure changes.

Do not:

- reintroduce the side rail or side overlay panels;
- add marketing hero sections or decorative card-heavy pages;
- use gradient orbs or decorative bokeh backgrounds;
- use large display typography inside operational panels;
- add visible instructions explaining how to use normal controls;
- hide build-size issues by raising thresholds without a reason.

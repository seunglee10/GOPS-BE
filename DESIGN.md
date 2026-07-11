---
version: alpha-current
name: GOPS workspace design system
description: "A dark, dense market-analysis workspace built around a full-screen heatmap or chart canvas, compact flat panels, and a bottom agent command bar. The visual priority is repeat trading/research work: scan fast, compare panels, keep chrome quiet, and reserve accent color for action, focus, and live market state."
source:
  app: apps/gops-frontend
  styles: apps/gops-frontend/src/styles.css
  entry: apps/gops-frontend/index.html
updatedFor:
  font: Asta Sans
  typeScale: unified semantic typography roles
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
- Use the locked three-dark surface hierarchy, white/gray text, and one blue
  action color for selection and focus.
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
  header: "#1b1b1b"
  canvas: "#232323"
  chart-canvas: "#232323"
  panel: "#272727"
  panel-strong: "#272727"
  control: "#272727"
  ink: "#ffffff"
  ink-muted: "#999999"
  muted-soft: "#6f6f6f"
  hairline: "#262626"
  hairline-strong: "#333333"
  accent-blue: "#0099ff"
  accent-blue-active: "#33adff"
  point-yellow: "#fff436"
  point-orange: "#ff490a"
  point-purple: "#9c3dff"
  up: "#22c55e"
  down: "#ff5577"
  caution: "#ff7a3d"

layout:
  top-nav-height: 48px
  bottom-nav-height: 64px
  control-size: 36px
  app-ui-scale: 0.72
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
  border: "transparent"
  shadow: "none"
  filter: "none"
  note: "Core panels are flat solid surfaces; legacy glass variable names do not authorize glass effects."

typography:
  display-xl: "48px / 500 / 1.1 / 0"
  display-lg: "40px / 400 / 1.2 / 0"
  display-md: "32px / 400 / 1.2 / 0"
  title-lg: "24px / 400 / 1.35 / 0.12px"
  instrument-name: "24px / 700 / 1.2 / 0"
  title-md: "20px / 400 / 1.5 / 0"
  title-sm: "18px / 500 / 1.4 / 0"
  label-md: "16px / 500 / 1.4 / 0"
  button: "16px / 500 / 1.4 / 0"
  body-md: "14px / 400 / 1.25 / 0"
  caption: "14px / 500 / 1.35 / 0.16px"
  legal: "13.12px / 600 / 1.2 / 0"
  pricing-display: "44.8px / 475 / 1.1 / 0"
  pricing-section: "28px / 475 / 1.2 / 0"
  pricing-card-title: "20px / 475 / 1.3 / 0"
```

## Typography

The app loads Asta Sans from Google Fonts in `apps/gops-frontend/index.html`.
All primary font variables resolve to Asta Sans:

```css
--font-ui-serif: "Asta Sans", Arial, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
--font-ui-sans: "Asta Sans", Arial, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
--font-data-sans: "Asta Sans", Arial, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
```

The font family remains Asta Sans. The semantic roles below are the only
approved typography contract for the product. Each role is a complete,
indivisible style: size, weight, line height, letter spacing, and text transform
must travel together.

| Role | Size | Weight | Line height | Letter spacing | Transform | Reference example |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `display-xl` | 48px | 500 | 1.1 | 0 | none | Article h2: `Build the workspace` |
| `display-lg` | 40px | 400 | 1.2 | 0 | none | Homepage h1 hero: `All your teams, all their workflows` |
| `display-md` | 32px | 400 | 1.2 | 0 | none | Platform feature head: `Conversational app building` |
| `title-lg` | 24px | 400 | 1.35 | 0.12px | Section title: `Sophisticated workflows` |
| `instrument-name` | 24px | 700 | 1.2 | 0 | none | File-tab ticker or company name: `LLY` |
| `title-md` | 20px | 400 | 1.5 | 0 | none | Sub-section title: `Don't just talk. Deploy it.` |
| `title-sm` | 18px | 500 | 1.4 | 0 | none | Article-card title: `10 best AI app builders for 2026` |
| `label-md` | 16px | 500 | 1.4 | 0 | none | Demo-card title: `Production apps in prototype speed` |
| `button` | 16px | 500 | 1.4 | 0 | none | CTA label: `Get started for free` |
| `body-md` | 14px | 400 | 1.25 | 0 | none | `From scrappy startups to enterprise teams, Airtable adapts to whatever you need next without forcing a rebuild.` |
| `caption` | 14px | 500 | 1.35 | 0.16px | Meta label: `AI PROJECT PLANNING` |
| `legal` | 13.12px | 600 | 1.2 | 0 | none | Legal action: `Cookies Preferences` |
| `pricing-display` | 44.8px | 475 | 1.1 | 0 | none | Pricing h1: `A plan for every organization's needs` |
| `pricing-section` | 28px | 475 | 1.2 | 0 | none | Pricing section head: `Compare plans` |
| `pricing-card-title` | 20px | 475 | 1.3 | 0 | none | Tier name: `Business` |

GOPS component mapping:

| Component content | Required role |
| --- | --- |
| Article or editorial h2 | `display-xl` |
| Homepage hero h1 | `display-lg` |
| Platform feature head or dominant workspace value | `display-md` |
| Section title or featured instrument | `title-lg` |
| File-tab ticker or company name | `instrument-name` |
| Sub-section or major panel title | `title-md` |
| Article-card or prominent result title | `title-sm` |
| Demo-card, compact card, row, and utility title | `label-md` |
| CTA, tab, and text-action label | `button` |
| Body, footer, top navigation, input, and descriptive copy | `body-md` |
| Captions, chart axes, timestamps, metadata, status, and category text | `caption` |
| Cookie, attribution, and legal actions only | `legal` |
| Pricing-page h1 only | `pricing-display` |
| Pricing section heading only | `pricing-section` |
| Pricing tier name only | `pricing-card-title` |

Workspace panel sizing policy:

- Panel content and shared application chrome use the same compact local role
  scale. New panel content maps its role names onto those existing tokens rather
  than promoting every role globally.
- Headlines in flip-style news cards use the local `title-lg` role and wrap
  naturally up to three lines when the title exceeds the card width. Padding keeps
  the text clear of both the chart line and the bottom flip-progress strip;
  the chart is subdued beneath the headline region.
- The bottom Agent composer retains the existing compact control dimensions.

Enforcement rules:

- Asta Sans is mandatory for every role. Do not replace it or introduce a
  secondary display, body, data, or monospace family.
- Use the semantic role name through shared typography tokens, classes, or
  mixins. Do not declare local `font-size`, `font-weight`, `line-height`,
  `letter-spacing`, or `text-transform` values in a component.
- Do not mix the metrics from different roles. A `title-md` must always be
  `20px / 400 / 1.5 / 0`, for example.
- Do not add aliases, one-off roles, fluid type with `clamp()`, or intermediate
  values. A new role requires an explicit update to this document and the
  shared typography implementation in the same change.
- Canvas and SVG text must use matching shared numeric constants; renderer
  constraints are not an exception to the scale.
- Choose roles by meaning, not by the available size. Resolve hierarchy with
  the component mapping before using color or spacing as secondary cues.
- Keep the three `pricing-*` roles exclusive to pricing surfaces. Financial
  market values are not pricing-page content.
- `legal` is not a generic small-text role. Use it only for legal or attribution
  content; compact operational text remains `body-md` or `caption`.
- Preserve uppercase in source copy when the content requires it. No role
  forces capitalization.
- Prefer tabular numerals for market-data rows and chart readouts without
  changing the role's prescribed metrics.

## Layout

### App Shell

The app is a fixed full-viewport shell:

- `body`, `html`, and `#root` are `100vw`/`100vh` and `overflow: hidden`.
- `canvas-workspace` fills the viewport.
- The shell renders at `0.8` scale with an inverse-sized logical viewport,
  matching the proportions of Firefox at roughly 72% zoom. Keep the runtime constant,
  CSS fallback, and this document synchronized.
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

### Flat Panels

Most app panels share the same flat surface language:

```css
border: 0;
border-radius: var(--app-glass-radius);
background: var(--app-panel-glass-background);
box-shadow: none;
backdrop-filter: none;
```

`--app-panel-glass-background` is a compatibility name whose active value is
the solid panel color `#272727`. It does not authorize transparency, gradients,
blur, glow, or shadow. Structural surfaces use only the locked hierarchy:
`#1b1b1b` for the header and bottom command pill, `#232323` for the app/chart
canvas, and `#272727` for panels and controls. Do not invent nearby black or
gray surface colors.

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
- scrollbars are thin, neutral/white, and edge-aligned;
- chart panel controls stay inside the chart panel, not in global chrome.

### Treemap

The treemap is a full visual surface, not a card gallery:

- panel radius: 8px;
- background: the solid panel surface `#272727` without a decorative edge treatment;
- hover metadata appears top-left, compact and non-interactive;
- cell/tile radius is currently `0px` for heatmap cell geometry;
- up/down color uses semantic green/red, not decorative palette variants.
- sector and industry header bands reserve at least the assigned role's line
  box; labels are hidden when that height is unavailable;
- Korean and Latin labels are clipped to their own band or symbol cell and use
  a single Unicode ellipsis when width is constrained. Never shrink them below
  the approved typography roles or let them overlap adjacent tiles.

### Chart Workspace

Charts sit inside flat panel frames:

- chart canvas background is the solid app canvas color `#232323`;
- current price, comparison legends, drawing tools, and add-layer tools are
  overlays inside the chart panel;
- chart toolbar controls use icons and compact separators;
- chart drawing and add docks float just above the bottom command band.

### Index Panel

The `지수` panel follows the quiet flat-panel system:

- no backdrop blur on `market-indices-panel` or `index-widget-panel`;
- no decorative border or shell shadow;
- rotating index cards override `surface-raised` with a flat dark fill;
- sparkline area fill stays low opacity so the chart does not glow.

The index panel should read like quiet market data, not a highlighted
promotional tile.

## Navigation And Commands

### Top Nav

The top nav is quiet:

- fixed at top, 48px tall;
- solid `#1b1b1b` background with white content;
- active preset uses a white pill with black text;
- center slot is reserved for the preset dock in chart mode;
- right side contains only direct login/logout state.

There is no top alert/settings drawer trigger after the side-panel removal.

### Bottom Agent Command

The bottom command bar is the primary global command surface:

- fixed at bottom, 64px tall;
- transparent nav wrapper;
- centered `agent-box` spans the available width;
- pill radius (`999px`);
- solid `#1b1b1b` fill with a subtle white stroke and no blur or shadow;
- contains reference chips, input, send/stop button, and chat toggle.

The agent input placeholder changes by mode:

- chart mode: `Agent에게 물어보기`
- treemap mode: `기업명/티커로 차트 열기`
- unauthenticated when auth is required: `로그인 후 Agent를 사용할 수 있습니다`

### Chat Panel

The chat panel opens above the agent box:

- same solid `#272727` family as other panels;
- assistant/system messages sit in 14px rounded translucent message boxes;
- confidence is shown as a small tone dot when available;
- details/citations are collapsible to keep the command surface compact.

## Interaction States

Use color, not size, for most interaction feedback:

- hover/focus action color: `#0099ff`;
- active action color: `#0099ff`;
- chart/graph point accents: yellow `#fff436`, orange `#ff490a`, purple
  `#9c3dff`, green `#22c55e`, red `#ff5577`, and blue `#0099ff`;
- stop/destructive color: `#ff5577`;
- positive market state: `#22c55e`;
- warning/caution/reference highlight: `#ff7a3d`;
- disabled opacity: about `0.5` to `0.58`.

Avoid layout shift on hover. Controls should not resize when hovered, focused,
or active.

Point accents distinguish series, markers, annotations, or graph points. Green,
red, and blue may be used as point accents when they do not create ambiguity
with bullish, bearish, or primary-action meaning. Point accents are never
structural surface colors.

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

- Inputs are borderless inside a pill or flat container.
- Symbol search menus use the solid panel surface and stay local to the invoking
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
- agent input remains `body-md` and text actions remain `button`;
- side rail remains absent on all breakpoints;
- panel text must stay inside its bounds.

Do not scale fonts continuously with viewport or container width and do not
override a role's metrics at a breakpoint. Responsive components may switch to
a different approved role only when their semantic hierarchy also changes.
Prefer layout constraints and wrapping before changing roles.

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
- use the locked `#1b1b1b` header, `#232323` canvas/chart, and `#272727` panel hierarchy;
- use `#ffffff` as the only primary white and blue for action, focus, selection,
  and current-price emphasis;
- use flat panels with 8px radius, no decorative border, no blur, and no shadow;
- use the bottom agent command as the primary global command surface;
- keep chart and layout tools close to the panel they affect;
- verify `npm run build` after UI structure changes.

Do not:

- reintroduce the side rail or side overlay panels;
- add marketing hero sections or decorative card-heavy pages;
- introduce arbitrary structural black/gray variants or translucent-black surfaces;
- add structural gradients, blur, glow, or shadows to create surface hierarchy;
- use gradient orbs or decorative bokeh backgrounds;
- repurpose `legal` or any `pricing-*` role as a generic small or financial-value style;
- introduce typography outside the approved semantic roles;
- add visible instructions explaining how to use normal controls;
- hide build-size issues by raising thresholds without a reason.

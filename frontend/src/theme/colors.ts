export type ThemeColorToken =
  | "background"
  | "surface"
  | "surfaceStrong"
  | "text"
  | "muted"
  | "border"
  | "shadow"
  | "up"
  | "upSoft"
  | "down"
  | "downSoft"
  | "changeUp"
  | "changeDown"
  | "ma5"
  | "ma20"
  | "ma60"
  | "drawing"
  | "preview"
  | "footprint"
  | "grid"
  | "axis"
  | "crosshair"
  | "tileText"
  | "tileTextInverse";

export type ThemeColors = Record<ThemeColorToken, string> & {
  palette: Set<string>;
};

const cssVariableByToken: Record<ThemeColorToken, string> = {
  background: "--color-background",
  surface: "--color-surface",
  surfaceStrong: "--color-surface-strong",
  text: "--color-text",
  muted: "--color-muted",
  border: "--color-border",
  shadow: "--color-shadow",
  up: "--color-up",
  upSoft: "--color-up-soft",
  down: "--color-down",
  downSoft: "--color-down-soft",
  changeUp: "--color-change-up",
  changeDown: "--color-change-down",
  ma5: "--color-ma5",
  ma20: "--color-ma20",
  ma60: "--color-ma60",
  drawing: "--color-drawing",
  preview: "--color-preview",
  footprint: "--color-footprint",
  grid: "--color-grid",
  axis: "--color-axis",
  crosshair: "--color-crosshair",
  tileText: "--color-tile-text",
  tileTextInverse: "--color-tile-text-inverse"
};

const paletteVariables = [
  "--gops-black",
  "--gops-white",
  "--gops-ink",
  "--gops-background",
  "--gops-umber",
  "--gops-umber-soft",
  "--gops-violet",
  "--gops-violet-soft",
  "--gops-crimson",
  "--gops-crimson-soft",
  "--gops-teal",
  "--gops-teal-soft",
  "--gops-moss",
  "--gops-moss-soft"
];

export function readThemeColors(): ThemeColors {
  const root = getComputedStyle(document.documentElement);
  const read = (name: string) => root.getPropertyValue(name).trim();
  const fallback = read("--gops-ink");
  const palette = new Set(paletteVariables.map(read).filter(Boolean).map((value) => value.toLowerCase()));
  const colors = Object.fromEntries(
    Object.entries(cssVariableByToken).map(([token, variable]) => [token, read(variable) || fallback])
  ) as Record<ThemeColorToken, string>;
  return { ...colors, palette };
}

export function resolveThemeColor(theme: ThemeColors, token: ThemeColorToken): string {
  return theme[token] || theme.text;
}

export function resolveRawPaletteColor(theme: ThemeColors, rawColor: string | undefined, fallback: ThemeColorToken): string {
  if (rawColor && theme.palette.has(rawColor.toLowerCase())) {
    return rawColor;
  }
  return resolveThemeColor(theme, fallback);
}

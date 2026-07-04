import { Search } from "lucide-react";
import { type CSSProperties, useEffect, useMemo, useState } from "react";
import type { ChartSymbolDto } from "../chart/types";

type SymbolSearchProps = {
  symbols: ChartSymbolDto[];
  selectedSymbol?: string;
  selectedLabel?: string;
  placeholder?: string;
  compact?: boolean;
  className?: string;
  style?: CSSProperties;
  onSelectSymbol: (symbol: string) => void;
  onPointerActivity?: () => void;
  formatSelectedLabel?: (symbol: ChartSymbolDto) => string;
};

export function SymbolSearch({
  symbols,
  selectedSymbol,
  selectedLabel,
  placeholder = "종목 검색",
  compact = false,
  className,
  style,
  onSelectSymbol,
  onPointerActivity,
  formatSelectedLabel
}: SymbolSearchProps) {
  const [query, setQuery] = useState(selectedLabel ?? "");
  const [open, setOpen] = useState(false);
  const [focused, setFocused] = useState(false);
  const fallbackLabel = selectedSymbol ?? "";
  const selectedDisplayLabel = selectedLabel ?? fallbackLabel;

  const filteredSymbols = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return rankSymbolMatches(symbols, normalizedQuery).slice(0, 10);
  }, [query, symbols]);

  useEffect(() => {
    if (!open) {
      setQuery(selectedDisplayLabel);
    }
  }, [open, selectedDisplayLabel]);

  const selectSymbol = (symbol: ChartSymbolDto) => {
    setQuery(formatSymbolLabel(symbol, formatSelectedLabel));
    setOpen(false);
    setFocused(false);
    onSelectSymbol(symbol.symbol);
  };

  const submitFirstMatch = (rawQuery = query) => {
    const normalizedQuery = rawQuery.trim().toLowerCase();
    const matches = rankSymbolMatches(symbols, normalizedQuery);
    const exact = matches.find((symbol) => symbol.symbol.toLowerCase() === normalizedQuery);
    const next = exact ?? matches[0];
    if (next) {
      selectSymbol(next);
    }
  };

  const active = open || focused;

  return (
    <div
      className={[
        "symbol-search",
        compact ? "symbol-search-compact" : "",
        "surface-flat",
        active ? "surface-recessed is-active" : "",
        className
      ].filter(Boolean).join(" ")}
      style={style}
      onPointerEnter={onPointerActivity}
      onPointerMove={onPointerActivity}
      onBlur={(event) => {
        const relatedTarget = event.relatedTarget;
        if (!(relatedTarget instanceof Node) || !event.currentTarget.contains(relatedTarget)) {
          setFocused(false);
          setOpen(false);
          setQuery(selectedDisplayLabel);
        }
      }}
    >
      <input
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onFocus={() => {
          setFocused(true);
          setQuery("");
          setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            submitFirstMatch(event.currentTarget.value);
          }
          if (event.key === "Escape") {
            setOpen(false);
            setFocused(false);
            setQuery(selectedDisplayLabel);
          }
        }}
        placeholder={placeholder}
        aria-label="Symbol search"
        autoComplete="off"
      />
      <button
        type="button"
        aria-label="Open symbol dropdown"
        title="Open symbol dropdown"
        onClick={() => {
          if (open) {
            setOpen(false);
            setFocused(false);
            setQuery(selectedDisplayLabel);
            return;
          }
          setFocused(true);
          setQuery("");
          setOpen(true);
        }}
      >
        <Search size={compact ? 12 : 14} aria-hidden="true" />
      </button>
      {open && (
        <div className="symbol-search-menu surface-flat surface-recessed" role="listbox" aria-label="Symbols">
          {filteredSymbols.map((symbol) => (
            <button
              key={symbol.symbol}
              type="button"
              className={symbol.symbol === selectedSymbol ? "active" : ""}
              role="option"
              aria-selected={symbol.symbol === selectedSymbol}
              onPointerDown={(event) => {
                event.preventDefault();
                selectSymbol(symbol);
              }}
            >
              <strong>{symbol.symbol}</strong>
              <span>{symbol.name}</span>
            </button>
          ))}
          {!filteredSymbols.length && <p>검색 결과 없음</p>}
        </div>
      )}
    </div>
  );
}

function formatSymbolLabel(
  symbol: ChartSymbolDto,
  formatSelectedLabel?: (symbol: ChartSymbolDto) => string
): string {
  return formatSelectedLabel?.(symbol) ?? `${symbol.symbol} - ${symbol.name}`;
}

function rankSymbolMatches(symbols: ChartSymbolDto[], query: string): ChartSymbolDto[] {
  const matches = query
    ? symbols.filter((symbol) => (
        symbol.symbol.toLowerCase().includes(query) ||
        symbol.name.toLowerCase().includes(query) ||
        symbol.sector?.toLowerCase().includes(query)
      ))
    : symbols;
  return [...matches].sort((left, right) => symbolSearchRank(left, query) - symbolSearchRank(right, query));
}

function symbolSearchRank(symbol: ChartSymbolDto, query: string): number {
  if (!query) {
    return 0;
  }
  const ticker = symbol.symbol.toLowerCase();
  const name = symbol.name.toLowerCase();
  if (ticker === query) {
    return 0;
  }
  if (ticker.startsWith(query)) {
    return 1;
  }
  if (name.startsWith(query)) {
    return 2;
  }
  if (ticker.includes(query)) {
    return 3;
  }
  if (name.includes(query)) {
    return 4;
  }
  return 5;
}

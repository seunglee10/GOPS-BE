import { useEffect, useMemo, useState } from "react";
import type { ChartSymbolDto } from "../chart/types";

type SymbolSearchProps = {
  symbols: ChartSymbolDto[];
  selectedSymbol?: string;
  selectedLabel?: string;
  className?: string;
  buttonLabel?: string;
  onSelectSymbol: (symbol: string) => void;
  onPointerActivity?: () => void;
};

export function SymbolSearch({
  symbols,
  selectedSymbol,
  selectedLabel,
  className,
  buttonLabel,
  onSelectSymbol,
  onPointerActivity
}: SymbolSearchProps) {
  const [query, setQuery] = useState(selectedLabel ?? "");
  const [open, setOpen] = useState(false);

  const filteredSymbols = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return rankSymbolMatches(symbols, normalizedQuery).slice(0, 10);
  }, [query, symbols]);

  useEffect(() => {
    if (!open) {
      setQuery(selectedLabel ?? "");
    }
  }, [open, selectedLabel]);

  const selectSymbol = (symbol: ChartSymbolDto) => {
    setQuery(`${symbol.symbol} - ${symbol.name}`);
    setOpen(false);
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

  return (
    <div
      className={["symbol-search", className].filter(Boolean).join(" ")}
      onPointerEnter={onPointerActivity}
      onPointerMove={onPointerActivity}
    >
      <input
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onFocus={() => {
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
            setQuery(selectedLabel ?? "");
          }
        }}
        placeholder="종목 검색"
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
            setQuery(selectedLabel ?? "");
            return;
          }
          setQuery("");
          setOpen(true);
        }}
      >
        {buttonLabel ?? selectedSymbol ?? "S&P500"}
      </button>
      {open && (
        <div className="symbol-search-menu" role="listbox" aria-label="Symbols">
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

import { ChevronDown, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { SupportedSymbol, WatchlistSymbol } from "@gops/chart-engine/symbols";
import { makeCommand } from "../layout/commands";
import type { LayoutCommand } from "../layout/types";
import { SystemOrbRail } from "./SystemArea";

type TopAppBarProps = {
  aiActive: boolean;
  watchlistActive: boolean;
  settingsActive: boolean;
  notificationsActive: boolean;
  activeSymbol: SupportedSymbol;
  symbolOptions: readonly WatchlistSymbol[];
  symbolSearchError?: string;
  onToggleNotifications: () => void;
  onTogglePrimaryAgent: () => void;
  onToggleWatchlist: () => void;
  onToggleSettings: () => void;
  onSymbolQueryChange: (query: string) => void;
  onSymbolOptionsRequest: (query: string) => void;
  onSymbolSearch: (symbol: string) => boolean;
  onCommand: (command: LayoutCommand) => void;
};

function isInteractiveTopBarTarget(target: EventTarget | null): boolean {
  const element = target instanceof Element ? target : null;
  return Boolean(element?.closest("button, input, textarea, select, option, datalist, form, a, [role='button']"));
}

export function TopAppBar({
  aiActive,
  watchlistActive,
  settingsActive,
  notificationsActive,
  activeSymbol,
  symbolOptions,
  symbolSearchError,
  onToggleNotifications,
  onTogglePrimaryAgent,
  onToggleWatchlist,
  onToggleSettings,
  onSymbolQueryChange,
  onSymbolOptionsRequest,
  onSymbolSearch,
  onCommand
}: TopAppBarProps) {
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const [searchDraft, setSearchDraft] = useState<string>(activeSymbol);
  const [symbolDropdownQuery, setSymbolDropdownQuery] = useState<string>(activeSymbol);
  const [symbolDropdownOpen, setSymbolDropdownOpen] = useState(false);
  const filteredSymbolOptions = useMemo(() => {
    const query = symbolDropdownQuery.trim().toUpperCase();
    return symbolOptions
      .filter((item) => !query || item.symbol.includes(query) || item.name.toUpperCase().includes(query))
      .slice(0, 40);
  }, [symbolDropdownQuery, symbolOptions]);

  useEffect(() => {
    setSearchDraft(activeSymbol);
  }, [activeSymbol]);

  const submitSymbol = (value: string) => {
    if (onSymbolSearch(value)) {
      setSymbolDropdownOpen(false);
    }
  };

  const readSearchInputValue = () => searchInputRef.current?.value ?? searchDraft;

  return (
    <header
      className="top-app-bar"
      onClick={(event) => {
        if (!isInteractiveTopBarTarget(event.target)) {
          onCommand(makeCommand("layout.panel.select", "user", { clear: true }));
        }
      }}
    >
      <form
        className={symbolSearchError ? "brand-search has-error" : "brand-search"}
        onSubmit={(event) => {
          event.preventDefault();
          const submittedSymbol = new FormData(event.currentTarget).get("symbolSearch");
          submitSymbol(typeof submittedSymbol === "string" ? submittedSymbol : searchDraft);
        }}
        onBlur={(event) => {
          const nextTarget = event.relatedTarget;
          if (!(nextTarget instanceof Node) || !event.currentTarget.contains(nextTarget)) {
            setSymbolDropdownOpen(false);
          }
        }}
      >
        <span className="brand-mark">GOPS</span>
        <span className="search-divider" />
        <input
          ref={searchInputRef}
          name="symbolSearch"
          value={searchDraft}
          placeholder="종목 검색"
          aria-label="종목 검색"
          aria-invalid={Boolean(symbolSearchError)}
          title={symbolSearchError ?? "종목 코드 검색"}
          onChange={(event) => {
            const value = event.target.value.toUpperCase();
            setSearchDraft(value);
            if (symbolDropdownOpen) {
              setSymbolDropdownQuery(value);
            }
            onSymbolQueryChange(value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.nativeEvent.isComposing) {
              event.preventDefault();
              submitSymbol(event.currentTarget.value);
            }
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setSymbolDropdownOpen(true);
            }
            if (event.key === "Escape") {
              setSymbolDropdownOpen(false);
            }
          }}
        />
        <button
          type="button"
          className={symbolDropdownOpen ? "search-dropdown-button active" : "search-dropdown-button"}
          title="검색 가능한 종목 보기"
          aria-label="검색 가능한 종목 보기"
          aria-expanded={symbolDropdownOpen}
          onClick={() => {
            const value = readSearchInputValue().toUpperCase();
            const query = value === activeSymbol ? "" : value;
            setSearchDraft(value);
            setSymbolDropdownQuery(query);
            onSymbolOptionsRequest(query);
            setSymbolDropdownOpen((open) => !open);
          }}
        >
          <ChevronDown size={15} aria-hidden="true" />
        </button>
        <button type="submit" className="search-submit-button" title="종목 검색">
          <Search size={15} aria-hidden="true" />
        </button>
        {symbolDropdownOpen && (
          <div className="symbol-search-dropdown" role="listbox" aria-label="검색 가능한 종목">
            {filteredSymbolOptions.map((item) => (
              <button
                key={item.symbol}
                type="button"
                role="option"
                className="symbol-search-option"
                aria-selected={item.symbol === activeSymbol}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  setSearchDraft(item.symbol);
                  setSymbolDropdownQuery(item.symbol);
                  onSymbolQueryChange(item.symbol);
                  submitSymbol(item.symbol);
                }}
              >
                <strong>{item.symbol}</strong>
                <span>{item.name}</span>
              </button>
            ))}
            {filteredSymbolOptions.length === 0 && (
              <span className="symbol-search-empty">일치하는 종목이 없습니다</span>
            )}
          </div>
        )}
        {symbolSearchError && <span className="search-error-message">{symbolSearchError}</span>}
      </form>

      <SystemOrbRail
        aiActive={aiActive}
        watchlistActive={watchlistActive}
        settingsActive={settingsActive}
        notificationsActive={notificationsActive}
        onTogglePrimaryAgent={onTogglePrimaryAgent}
        onToggleWatchlist={onToggleWatchlist}
        onToggleNotifications={onToggleNotifications}
        onToggleSettings={onToggleSettings}
      />
    </header>
  );
}

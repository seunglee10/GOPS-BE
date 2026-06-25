import { CandlestickChart, Clock3 } from "lucide-react";
import { DEFAULT_WATCHLIST } from "../types/documents";
import type { Timeframe } from "../types/market";

interface ToolbarProps {
  symbol: string;
  timeframe: Timeframe;
  onSymbolChange(symbol: string): void;
  onTimeframeChange(timeframe: Timeframe): void;
}

const TIMEFRAMES: Timeframe[] = ["1s", "5s", "15s", "1m", "5m", "15m", "1h", "1d"];

export function Toolbar({ symbol, timeframe, onSymbolChange, onTimeframeChange }: ToolbarProps): JSX.Element {
  return (
    <div className="toolbar" aria-label="Chart toolbar">
      <label className="select-control">
        <CandlestickChart size={16} />
        <select value={symbol} onChange={(event) => onSymbolChange(event.target.value)} aria-label="Symbol">
          {DEFAULT_WATCHLIST.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </label>
      <label className="select-control">
        <Clock3 size={16} />
        <select value={timeframe} onChange={(event) => onTimeframeChange(event.target.value as Timeframe)} aria-label="Timeframe">
          {TIMEFRAMES.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

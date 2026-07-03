export type MarketTickerItem = {
  symbol: string;
  label?: string;
  changePercent: number;
};

const defaultTickerItems: MarketTickerItem[] = [
  { symbol: "S&P 500", changePercent: 0.42 },
  { symbol: "NASDAQ", changePercent: 0.68 },
  { symbol: "DOW", changePercent: -0.12 },
  { symbol: "RUSSELL", changePercent: 0.25 },
  { symbol: "VIX", changePercent: -1.84 },
  { symbol: "KOSPI", changePercent: 0.31 },
  { symbol: "USD/KRW", changePercent: -0.18 }
];

function formatPercent(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

export function MarketTicker({ items = defaultTickerItems }: { items?: MarketTickerItem[] }) {
  return (
    <div className="market-ticker" aria-label="시장 지표 요약">
      <div className="market-ticker-position">
        <div className="market-ticker-track">
          <span className="market-ticker-group">
            <TickerItems items={items} />
          </span>
          <span className="market-ticker-group" aria-hidden="true">
            <TickerItems items={items} />
          </span>
        </div>
      </div>
    </div>
  );
}

function TickerItems({ items }: { items: MarketTickerItem[] }) {
  return (
    <>
      {items.map((item) => (
        <span key={item.symbol} className="market-ticker-item">
          <strong>{item.label ?? item.symbol}</strong>
          <span className={item.changePercent >= 0 ? "market-up" : "market-down"}>
            {formatPercent(item.changePercent)}
          </span>
        </span>
      ))}
    </>
  );
}

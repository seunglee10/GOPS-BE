import type { Provider } from "../types/market";

export interface MarketProviderDefinition {
  provider: Provider;
  label: string;
  runtime: "dummy" | "external";
}

export const defaultMarketProviderRegistry: Record<Provider, MarketProviderDefinition> = {
  dummy: { provider: "dummy", label: "Dummy Market Stream", runtime: "dummy" },
  alpaca: { provider: "alpaca", label: "Alpaca", runtime: "external" }
};

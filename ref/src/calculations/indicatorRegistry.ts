import {
  DEFAULT_INDICATOR_PRESETS,
  calculateAtr,
  calculateBollingerBands,
  calculateEma,
  calculateMacd,
  calculateRsi,
  calculateSma,
  calculateVolumeMa,
  calculateVwap
} from "./indicators";
import type { CalculationInputs, IndicatorDefinition, IndicatorRegistry, IndicatorType } from "../types/calculations";
import type { CommandValidationError } from "../types/commands";

export const ENABLED_INDICATORS: IndicatorType[] = [
  "SMA",
  "EMA",
  "RSI",
  "MACD",
  "BOLLINGER_BANDS",
  "VWAP",
  "ATR",
  "VOLUME_MA"
];

function periodValidator(periodKey = "period", min = 1, max = 500) {
  return (inputs: CalculationInputs): CommandValidationError[] => {
    const value = inputs[periodKey];
    if (typeof value !== "number" || value < min || value > max) {
      return [{ code: "invalid_payload", message: `${periodKey} must be between ${min} and ${max}.`, path: `payload.node.inputs.${periodKey}` }];
    }
    return [];
  };
}

function mergeValidators(...validators: Array<(inputs: CalculationInputs) => CommandValidationError[]>) {
  return (inputs: CalculationInputs) => validators.flatMap((validator) => validator(inputs));
}

function definition(definition: IndicatorDefinition): IndicatorDefinition {
  return definition;
}

export const defaultIndicatorRegistry: IndicatorRegistry = {
  SMA: definition({
    type: "SMA",
    label: "Simple Moving Average",
    preferredPane: "price",
    defaultInputs: DEFAULT_INDICATOR_PRESETS.SMA,
    validateInputs: periodValidator(),
    calculate: ({ candles, node }) => calculateSma(candles, node)
  }),
  EMA: definition({
    type: "EMA",
    label: "Exponential Moving Average",
    preferredPane: "price",
    defaultInputs: DEFAULT_INDICATOR_PRESETS.EMA,
    validateInputs: periodValidator(),
    calculate: ({ candles, node }) => calculateEma(candles, node)
  }),
  RSI: definition({
    type: "RSI",
    label: "Relative Strength Index",
    preferredPane: "indicator",
    defaultInputs: DEFAULT_INDICATOR_PRESETS.RSI,
    validateInputs: periodValidator(),
    calculate: ({ candles, node }) => calculateRsi(candles, node)
  }),
  MACD: definition({
    type: "MACD",
    label: "MACD",
    preferredPane: "indicator",
    defaultInputs: DEFAULT_INDICATOR_PRESETS.MACD,
    validateInputs: mergeValidators(periodValidator("fastPeriod"), periodValidator("slowPeriod"), periodValidator("signalPeriod")),
    calculate: ({ candles, node }) => calculateMacd(candles, node)
  }),
  BOLLINGER_BANDS: definition({
    type: "BOLLINGER_BANDS",
    label: "Bollinger Bands",
    preferredPane: "price",
    defaultInputs: DEFAULT_INDICATOR_PRESETS.BOLLINGER_BANDS,
    validateInputs: mergeValidators(periodValidator(), (inputs) =>
      typeof inputs.standardDeviation === "number" && inputs.standardDeviation > 0 && inputs.standardDeviation <= 10
        ? []
        : [
            {
              code: "invalid_payload",
              message: "standardDeviation must be between 0 and 10.",
              path: "payload.node.inputs.standardDeviation"
            }
          ]
    ),
    calculate: ({ candles, node }) => calculateBollingerBands(candles, node)
  }),
  VWAP: definition({
    type: "VWAP",
    label: "VWAP",
    preferredPane: "price",
    defaultInputs: DEFAULT_INDICATOR_PRESETS.VWAP,
    validateInputs: () => [],
    calculate: ({ candles, node }) => calculateVwap(candles, node)
  }),
  ATR: definition({
    type: "ATR",
    label: "Average True Range",
    preferredPane: "indicator",
    defaultInputs: DEFAULT_INDICATOR_PRESETS.ATR,
    validateInputs: periodValidator(),
    calculate: ({ candles, node }) => calculateAtr(candles, node)
  }),
  VOLUME_MA: definition({
    type: "VOLUME_MA",
    label: "Volume Moving Average",
    preferredPane: "volume",
    defaultInputs: DEFAULT_INDICATOR_PRESETS.VOLUME_MA,
    validateInputs: periodValidator(),
    calculate: ({ candles, node }) => calculateVolumeMa(candles, node)
  })
};

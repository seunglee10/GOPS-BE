from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


CONTENT_TASK_TYPES = ("news", "chart", "macro", "ontology", "financial", "financial_comparison", "market_move", "general")
ROUTE_MODES = ("analysis", "ui_layout", "hybrid", "clarify")
ROLE_ORDER = ("chart", "news", "macro", "ontology", "financial")
DEFAULT_ANALYSIS_ROLES = ("chart", "news", "macro", "ontology")
UI_ACTIONS = (
    "focus",
    "resize",
    "move",
    "open",
    "close",
    "arrange",
    "keep",
    "load",
    "tidy",
    "undo",
    "reset",
    "swap",
    "replace",
    "pin",
    "unpin",
    "save",
)
# Keep in sync with AgentLayoutPanelType (gops-frontend/src/layout/agentLayoutTypes.ts)
# and PANEL_TYPES in orchestration/ui_intent.py.
UI_PANEL_TYPES = (
    "chart",
    "compareChart",
    "marketIndices",
    "companyProfile",
    "companyMulti",
    "companyValuation",
    "companyProfitability",
    "companyStability",
    "popularStocks",
    "newsFeed",
    "indicatorCompare",
    "orderTicket",
    "orderFlowProfile",
    "portfolioDashboard",
    "portfolioHoldings",
    "portfolioMulti",
    "portfolioInvestment",
    "portfolioPerformance",
    "portfolioInvested",
    "portfolioDividend",
    "portfolioDiversification",
    "stockRecommendations",
    "themeRadar",
    "aiSummary",
    "ontologyGraph",
)
UI_SIZE_INTENTS = ("max", "large", "small", "min")
UI_POSITION_INTENTS = ("top", "bottom", "left", "right", "center")
UI_RELATION_INTENTS = ("left", "right", "above", "below", "beside")
UI_LAYOUT_PRESETS = ("default_workspace", "visible_workspace")

ROLES_BY_CONTENT_TASK = {
    "news": ("news",),
    "chart": ("chart",),
    "macro": ("macro",),
    "ontology": ("ontology",),
    "financial": ("financial",),
    "financial_comparison": ("financial",),
    "market_move": DEFAULT_ANALYSIS_ROLES,
    "general": DEFAULT_ANALYSIS_ROLES,
}

INTENT_TYPE_BY_CONTENT_TASK = {
    "news": "news",
    "chart": "chart",
    "macro": "macro",
    "ontology": "ontology",
    "financial": "financial-analysis",
    "financial_comparison": "financial-comparison",
    "market_move": "market-move",
    "general": "general-analysis",
}


@dataclass
class ContentTask:
    taskType: str
    confidence: float = 0.5
    source: str = "rule"
    reason: str = ""
    targetEntityText: str | None = None
    roles: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.taskType not in CONTENT_TASK_TYPES:
            self.taskType = "general"
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if not self.roles:
            self.roles = list(ROLES_BY_CONTENT_TASK.get(self.taskType, ROLE_ORDER))
        self.roles = [role for role in ROLE_ORDER if role in set(self.roles)]

    @property
    def intent_type(self) -> str:
        return INTENT_TYPE_BY_CONTENT_TASK.get(self.taskType, "general-analysis")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UiTask:
    action: str
    confidence: float = 0.5
    source: str = "ui-fallback"
    reason: str = ""
    targetPanelType: str | None = None
    targetPanelId: str | None = None
    targetPanelTypes: list[str] = field(default_factory=list)
    targetPanelIds: list[str] = field(default_factory=list)
    targetAll: bool = False
    replacePanelType: str | None = None
    replacePanelId: str | None = None
    anchorPanelType: str | None = None
    anchorPanelId: str | None = None
    relationIntent: str | None = None
    layoutPreset: str | None = None
    presetId: str | None = None
    presetName: str | None = None
    presetKind: str | None = None
    sizeIntent: str | None = None
    sizeFraction: float | None = None
    positionIntent: str | None = None
    chartAction: str | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        if self.action not in UI_ACTIONS:
            self.action = "focus"
        if self.targetPanelType not in UI_PANEL_TYPES:
            self.targetPanelType = None
        self.targetPanelTypes = [panel_type for panel_type in self.targetPanelTypes if panel_type in UI_PANEL_TYPES]
        if self.targetPanelType and self.targetPanelType not in self.targetPanelTypes:
            self.targetPanelTypes.insert(0, self.targetPanelType)
        if self.targetPanelTypes and not self.targetPanelType:
            self.targetPanelType = self.targetPanelTypes[0]
        self.targetPanelIds = [str(panel_id).strip() for panel_id in self.targetPanelIds if str(panel_id or "").strip()]
        if self.targetPanelId and self.targetPanelId not in self.targetPanelIds:
            self.targetPanelIds.insert(0, self.targetPanelId)
        if self.targetPanelIds and not self.targetPanelId:
            self.targetPanelId = self.targetPanelIds[0]
        self.targetAll = bool(self.targetAll)
        if self.replacePanelType not in UI_PANEL_TYPES:
            self.replacePanelType = None
        self.replacePanelId = str(self.replacePanelId or "").strip() or None
        if self.anchorPanelType not in UI_PANEL_TYPES:
            self.anchorPanelType = None
        self.anchorPanelId = str(self.anchorPanelId or "").strip() or None
        if self.relationIntent not in UI_RELATION_INTENTS:
            self.relationIntent = None
        if self.layoutPreset not in UI_LAYOUT_PRESETS:
            self.layoutPreset = None
        self.presetId = str(self.presetId or "").strip() or None
        self.presetName = str(self.presetName or "").strip() or None
        if self.presetKind not in {"default", "custom"}:
            self.presetKind = None
        if self.sizeIntent not in UI_SIZE_INTENTS:
            self.sizeIntent = None
        if self.sizeFraction is not None:
            try:
                self.sizeFraction = max(0.05, min(1.0, float(self.sizeFraction)))
            except (TypeError, ValueError):
                self.sizeFraction = None
        if self.positionIntent not in UI_POSITION_INTENTS:
            self.positionIntent = None
        if self.chartAction not in {"add", "replace"}:
            self.chartAction = None
        self.symbol = str(self.symbol or "").strip().upper() or None
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QueryUnderstanding:
    originalQuery: str
    normalizedQuery: str
    routeMode: str
    intentType: str
    selectedRoles: list[str] = field(default_factory=list)
    contentTasks: list[ContentTask] = field(default_factory=list)
    uiTasks: list[UiTask] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.5
    source: str = "fallback"
    needsClarification: bool = False
    warnings: list[str] = field(default_factory=list)
    resolvedSymbol: str | None = None
    resolvedSymbolSource: str | None = None
    newsTopic: str | None = None
    newsSymbols: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.routeMode not in ROUTE_MODES:
            self.routeMode = "analysis"
        self.selectedRoles = [role for role in ROLE_ORDER if role in set(self.selectedRoles)]
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "originalQuery": self.originalQuery,
            "normalizedQuery": self.normalizedQuery,
            "routeMode": self.routeMode,
            "intentType": self.intentType,
            "selectedRoles": list(self.selectedRoles),
            "contentTasks": [task.to_dict() for task in self.contentTasks],
            "uiTasks": [task.to_dict() for task in self.uiTasks],
            "entities": list(self.entities),
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
            "needsClarification": bool(self.needsClarification),
            "warnings": list(self.warnings),
            "resolvedSymbol": self.resolvedSymbol,
            "resolvedSymbolSource": self.resolvedSymbolSource,
            "newsTopic": self.newsTopic,
            "newsSymbols": list(self.newsSymbols),
            "timings": dict(self.timings),
        }


def normalize_query(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def selected_roles_for_tasks(tasks: list[ContentTask]) -> list[str]:
    selected = set()
    for task in tasks:
        selected.update(task.roles)
    return [role for role in ROLE_ORDER if role in selected]


def intent_type_for_tasks(tasks: list[ContentTask]) -> str:
    if not tasks:
        return "general-analysis"
    types = []
    for task in tasks:
        intent_type = task.intent_type
        if intent_type not in types:
            types.append(intent_type)
    if len(types) == 1:
        return types[0]
    return "+".join(types)

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from ..query_understanding.entity_resolver import fuzzy_ratio
from ..query_understanding.korean_text import choseong_key, compact_text, jamo_key, normalize_query_text, similarity
from .schema import UI_PANEL_TYPES, UiTask


DEFAULT_WORKSPACE_PANEL_TYPES = ["chart", "newsFeed", "aiSummary"]
DEFAULT_UI_LEXICON_PATH = Path(__file__).resolve().parents[3] / "config" / "ui-intent-lexicon.json"
TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣ㄱ-ㅎㅏ-ㅣ.]+")
CLAUSE_SPLIT_RE = re.compile(r"\s*(?:그리고|또|및|[,.!?;]|&|\band\b|(?<=주고)\s+)\s*", re.IGNORECASE)


class UiTaskList(list):
    def __init__(
        self,
        tasks: Iterable[UiTask] = (),
        *,
        needs_classifier: bool = False,
        warnings: Iterable[str] = (),
        confidence: float = 0.0,
    ):
        super().__init__(tasks)
        self.needs_classifier = bool(needs_classifier)
        self.warnings = list(warnings)
        self.confidence = float(confidence)


@dataclass
class UiParseResult:
    tasks: list[UiTask] = field(default_factory=list)
    needs_classifier: bool = False
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def task_list(self) -> UiTaskList:
        return UiTaskList(
            self.tasks,
            needs_classifier=self.needs_classifier,
            warnings=self.warnings,
            confidence=self.confidence,
        )


@dataclass(frozen=True)
class MorphToken:
    text: str
    index: int
    compact: str
    jamo: str
    choseong: str
    variants: tuple[str, ...]


@dataclass(frozen=True)
class Clause:
    text: str
    index: int
    compact: str
    tokens: tuple[MorphToken, ...]


@dataclass(frozen=True)
class AliasMatch:
    value: str
    alias: str
    score: float
    token_index: int
    source: str
    strength: str = "strong"


class DefaultMorphAnalyzer:
    def clauses(self, query: Any) -> list[Clause]:
        text = normalize_query_text(query)
        parts = [part.strip() for part in CLAUSE_SPLIT_RE.split(text) if part and part.strip()]
        if not parts and text:
            parts = [text]
        return [Clause(part, index, compact_text(part), tuple(self.tokens(part))) for index, part in enumerate(parts)]

    def tokens(self, text: str) -> list[MorphToken]:
        tokens = []
        for index, match in enumerate(TOKEN_RE.finditer(normalize_query_text(text))):
            surface = match.group(0)
            compact = compact_text(surface)
            variants = unique_texts([compact, *stem_variants(compact)])
            tokens.append(
                MorphToken(
                    text=surface,
                    index=index,
                    compact=compact,
                    jamo=jamo_key(compact),
                    choseong=choseong_key(compact),
                    variants=tuple(variants),
                )
            )
        return tokens


def parse_ui_query(query: Any, layout_context: dict[str, Any] | None = None) -> UiParseResult:
    lexicon = load_ui_lexicon()
    panels = compact_layout_panels_for_parser(layout_context if isinstance(layout_context, dict) else {}, lexicon)
    analyzer = DefaultMorphAnalyzer()
    tasks: list[UiTask] = []
    warnings: list[str] = []
    needs_classifier = False
    max_confidence = 0.0
    for clause in analyzer.clauses(query):
        clause_result = parse_ui_clause(clause, panels, lexicon)
        tasks.extend(clause_result.tasks)
        warnings.extend(clause_result.warnings)
        needs_classifier = needs_classifier or clause_result.needs_classifier
        max_confidence = max(max_confidence, clause_result.confidence)
    return UiParseResult(
        tasks=dedupe_ui_tasks(tasks),
        needs_classifier=needs_classifier,
        warnings=unique_texts(warnings),
        confidence=max_confidence,
    )


def parse_ui_clause(clause: Clause, panels: list[dict[str, Any]], lexicon: dict[str, Any]) -> UiParseResult:
    if not clause.compact:
        return UiParseResult()

    panel_matches = panel_matches_for_clause(clause, panels, lexicon)
    action_matches = value_matches(clause, lexicon.get("actions", {}), allow_short_fuzzy=True)
    size_matches = value_matches(clause, lexicon.get("sizes", {}), allow_short_fuzzy=True)
    position_matches = value_matches(clause, lexicon.get("positions", {}), allow_short_fuzzy=False)
    surface_matches = term_matches(clause, lexicon.get("surfaceNouns", []), allow_short_fuzzy=True)
    multi_matches = term_matches(clause, lexicon.get("multiPanelHints", []), allow_short_fuzzy=False)
    content_only_matches = term_matches(clause, lexicon.get("contentOnlyVerbs", []), allow_short_fuzzy=False)

    explicit_operation = has_layout_operation(action_matches, size_matches, position_matches, surface_matches, multi_matches)
    if is_content_only_clause(action_matches, size_matches, position_matches, surface_matches, multi_matches, content_only_matches):
        return UiParseResult(confidence=max_match_score(content_only_matches))

    multi_task = build_multi_panel_task(
        clause,
        panel_matches,
        action_matches,
        size_matches,
        position_matches,
        surface_matches,
        multi_matches,
        panels,
    )
    if multi_task is not None:
        return UiParseResult(tasks=[multi_task], confidence=multi_task.confidence)

    task = build_single_panel_task(
        clause,
        panel_matches,
        action_matches,
        size_matches,
        position_matches,
        surface_matches,
        multi_matches,
    )
    if task is not None:
        return UiParseResult(tasks=[task], confidence=task.confidence)

    strong_ui_signals = bool(
        surface_matches
        or any(match.strength == "strong" for match in action_matches)
        or size_matches
        or position_matches
        or multi_matches
    )
    ui_like = bool(panel_matches or strong_ui_signals or action_matches)
    incomplete_ui = ui_like and strong_ui_signals and not explicit_operation
    if incomplete_ui or (ui_like and explicit_operation):
        return UiParseResult(needs_classifier=True, warnings=["ui_parser_needs_classifier"], confidence=max_match_score([
            *panel_matches,
            *action_matches,
            *size_matches,
            *position_matches,
            *surface_matches,
            *multi_matches,
        ]))
    return UiParseResult(confidence=max_match_score(panel_matches))


def build_single_panel_task(
    clause: Clause,
    panel_matches: list[AliasMatch],
    action_matches: list[AliasMatch],
    size_matches: list[AliasMatch],
    position_matches: list[AliasMatch],
    surface_matches: list[AliasMatch],
    multi_matches: list[AliasMatch],
) -> UiTask | None:
    target = best_match(panel_matches)
    if target is None:
        return None
    strong_action = best_match([match for match in action_matches if match.strength == "strong"])
    weak_action = best_match([match for match in action_matches if match.strength != "strong"])
    size = best_size_match(size_matches)
    position = best_match(position_matches)
    has_surface_or_layout_hint = bool(surface_matches or multi_matches or position or size)
    if not strong_action and not has_surface_or_layout_hint:
        return None
    if weak_action and not strong_action and not has_surface_or_layout_hint:
        return None

    action = infer_action(strong_action, weak_action, size, position, multi_panel=False)
    if action is None:
        return None
    return UiTask(
        action=action,
        targetPanelType=target.value,
        targetPanelId=target.source.removeprefix("panel:") if target.source.startswith("panel:") else None,
        sizeIntent=size.value if size else None,
        positionIntent=position.value if position else None,
        confidence=ui_task_confidence([target, strong_action, weak_action, size, position, *surface_matches, *multi_matches]),
        source="ui-parser",
        reason=f"UI parser matched panel '{target.alias}' with layout operation in clause '{clause.text}'.",
    )


def build_multi_panel_task(
    clause: Clause,
    panel_matches: list[AliasMatch],
    action_matches: list[AliasMatch],
    size_matches: list[AliasMatch],
    position_matches: list[AliasMatch],
    surface_matches: list[AliasMatch],
    multi_matches: list[AliasMatch],
    panels: list[dict[str, Any]],
) -> UiTask | None:
    panel_types = unique_panel_types([match.value for match in panel_matches])
    generic_many = bool(multi_matches) and bool(surface_matches)
    if len(panel_types) < 2 and not generic_many:
        return None
    strong_action = best_match([match for match in action_matches if match.strength == "strong"])
    weak_action = best_match([match for match in action_matches if match.strength != "strong"])
    size = best_size_match(size_matches)
    position = best_match(position_matches)
    if not has_layout_operation(action_matches, size_matches, position_matches, surface_matches, multi_matches):
        return None
    action = infer_action(strong_action, weak_action, size, position, multi_panel=True) or "arrange"
    if action == "focus":
        action = "arrange"
    target_panel_types = panel_types if len(panel_types) >= 2 else list(DEFAULT_WORKSPACE_PANEL_TYPES)
    target_panel_ids = [panel["id"] for panel in panels if panel.get("type") in set(target_panel_types)]
    layout_preset = None if len(panel_types) >= 2 else "default_workspace"
    return UiTask(
        action=action,
        targetPanelTypes=target_panel_types,
        targetPanelIds=target_panel_ids,
        layoutPreset=layout_preset,
        sizeIntent=size.value if size else None,
        positionIntent=position.value if position else None,
        confidence=ui_task_confidence([*panel_matches, strong_action, weak_action, size, position, *surface_matches, *multi_matches]),
        source="ui-parser",
        reason=f"UI parser matched a multi-panel layout operation in clause '{clause.text}'.",
    )


def infer_action(
    strong_action: AliasMatch | None,
    weak_action: AliasMatch | None,
    size: AliasMatch | None,
    position: AliasMatch | None,
    *,
    multi_panel: bool,
) -> str | None:
    if size:
        return "resize"
    if strong_action and strong_action.value == "close":
        return "close"
    if multi_panel:
        if strong_action and strong_action.value in {"open", "arrange"}:
            return strong_action.value
        return "arrange"
    if position:
        return "move"
    if strong_action:
        return strong_action.value
    if weak_action:
        return "focus"
    return None


def has_layout_operation(
    action_matches: list[AliasMatch],
    size_matches: list[AliasMatch],
    position_matches: list[AliasMatch],
    surface_matches: list[AliasMatch],
    multi_matches: list[AliasMatch],
) -> bool:
    if size_matches or position_matches or multi_matches:
        return True
    if any(match.strength == "strong" for match in action_matches):
        return True
    return bool(surface_matches and action_matches)


def is_content_only_clause(
    action_matches: list[AliasMatch],
    size_matches: list[AliasMatch],
    position_matches: list[AliasMatch],
    surface_matches: list[AliasMatch],
    multi_matches: list[AliasMatch],
    content_only_matches: list[AliasMatch],
) -> bool:
    if not content_only_matches:
        return False
    if size_matches or position_matches or multi_matches or surface_matches:
        return False
    strong_layout_actions = [match for match in action_matches if match.strength == "strong" and match.value != "focus"]
    if not strong_layout_actions:
        return True
    return max_match_score(content_only_matches) >= 0.95 and max_match_score(strong_layout_actions) < 0.9


def panel_matches_for_clause(clause: Clause, panels: list[dict[str, Any]], lexicon: dict[str, Any]) -> list[AliasMatch]:
    matches: list[AliasMatch] = []
    for panel_type in UI_PANEL_TYPES:
        aliases = panel_aliases_for_type(panel_type, lexicon)
        match = best_alias_match(clause, aliases, allow_short_fuzzy=False)
        if match:
            matches.append(AliasMatch(panel_type, match.alias, match.score, match.token_index, f"type:{panel_type}"))
    for panel in panels:
        aliases = unique_texts([
            str(panel.get("title") or ""),
            *panel_aliases_for_type(str(panel.get("type") or ""), lexicon, panel.get("aliases")),
        ])
        match = best_alias_match(clause, aliases, allow_short_fuzzy=False)
        if match:
            matches.append(AliasMatch(str(panel["type"]), match.alias, match.score, match.token_index, f"panel:{panel['id']}"))
    return best_panel_matches(matches)


def value_matches(clause: Clause, group: Any, *, allow_short_fuzzy: bool) -> list[AliasMatch]:
    if not isinstance(group, dict):
        return []
    matches = []
    for value, spec in group.items():
        aliases = aliases_from_spec(spec)
        match = best_alias_match(clause, aliases, allow_short_fuzzy=allow_short_fuzzy)
        if match:
            strength = str(spec.get("strength") or "strong") if isinstance(spec, dict) else "strong"
            matches.append(AliasMatch(str(value), match.alias, match.score, match.token_index, "lexicon", strength))
    return sorted(matches, key=lambda item: (-item.score, item.token_index, item.value))


def term_matches(clause: Clause, terms: Any, *, allow_short_fuzzy: bool) -> list[AliasMatch]:
    if not isinstance(terms, list):
        return []
    matches = []
    for term in terms:
        match = best_alias_match(clause, [str(term)], allow_short_fuzzy=allow_short_fuzzy)
        if match:
            matches.append(AliasMatch(str(term), match.alias, match.score, match.token_index, "lexicon"))
    return sorted(matches, key=lambda item: (-item.score, item.token_index, item.value))


@dataclass(frozen=True)
class RawAliasMatch:
    alias: str
    score: float
    token_index: int


def best_alias_match(clause: Clause, aliases: Iterable[str], *, allow_short_fuzzy: bool) -> RawAliasMatch | None:
    best: RawAliasMatch | None = None
    for alias in aliases:
        alias_compact = compact_text(alias)
        if not alias_compact:
            continue
        exact = exact_alias_match(clause, alias_compact, alias)
        if exact and (best is None or exact.score > best.score):
            best = exact
        fuzzy = fuzzy_alias_match(clause, alias_compact, alias, allow_short_fuzzy=allow_short_fuzzy)
        if fuzzy and (best is None or fuzzy.score > best.score):
            best = fuzzy
    return best


def exact_alias_match(clause: Clause, alias_compact: str, alias: str) -> RawAliasMatch | None:
    for token in clause.tokens:
        if alias_compact in token.variants or any(alias_compact in variant for variant in token.variants if len(alias_compact) >= 2):
            return RawAliasMatch(alias, 0.98, token.index)
    if len(alias_compact) >= 2 and alias_compact in clause.compact:
        return RawAliasMatch(alias, min(0.99, 0.95 + len(alias_compact) / 100), 0)
    return None


def fuzzy_alias_match(clause: Clause, alias_compact: str, alias: str, *, allow_short_fuzzy: bool) -> RawAliasMatch | None:
    if len(alias_compact) < 2:
        return None
    if len(alias_compact) <= 2 and not allow_short_fuzzy:
        return None
    alias_jamo = jamo_key(alias_compact)
    alias_choseong = choseong_key(alias_compact)
    threshold = 0.74 if len(alias_compact) <= 2 else (0.8 if allow_short_fuzzy else 0.88)
    best: RawAliasMatch | None = None
    for token in clause.tokens:
        for variant in token.variants:
            if not variant or abs(len(variant) - len(alias_compact)) > 2:
                continue
            score = max(
                fuzzy_ratio(variant, alias_compact),
                fuzzy_ratio(jamo_key(variant), alias_jamo),
                similarity(choseong_key(variant), alias_choseong) if len(alias_choseong) >= 3 else 0.0,
            )
            if score < threshold:
                continue
            candidate = RawAliasMatch(alias, min(score, 0.93), token.index)
            if best is None or candidate.score > best.score:
                best = candidate
    return best


def stem_variants(compact: str) -> list[str]:
    variants = []
    suffixes = (
        "해주세요",
        "해주고",
        "해줘요",
        "해줘",
        "주세요",
        "주고",
        "줘요",
        "줘",
        "으로",
        "에게",
        "에서",
        "으로",
        "로",
        "에",
        "를",
        "을",
        "은",
        "는",
        "좀",
    )
    for suffix in suffixes:
        if compact.endswith(suffix) and len(compact) > len(suffix):
            variants.append(compact[: -len(suffix)])
    return variants


def compact_layout_panels_for_parser(layout_context: dict[str, Any], lexicon: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = lexicon or load_ui_lexicon()
    panels = layout_context.get("panels") if isinstance(layout_context, dict) else None
    if not isinstance(panels, list):
        return []
    compacted = []
    for item in panels:
        if not isinstance(item, dict):
            continue
        panel_id = str(item.get("id") or "").strip()
        panel_type = str(item.get("type") or "").strip()
        if not panel_id or panel_type not in UI_PANEL_TYPES:
            continue
        compacted.append({
            "id": panel_id,
            "type": panel_type,
            "title": str(item.get("title") or default_panel_title(panel_type, payload)),
            "aliases": panel_aliases_for_type(panel_type, payload, item.get("aliases")),
            "variant": str(item.get("variant") or ""),
            "layoutPinned": bool(item.get("layoutPinned")),
            "placement": item.get("placement") if isinstance(item.get("placement"), dict) else {},
        })
    return compacted


def panel_aliases_for_type(panel_type: str, lexicon: dict[str, Any] | None = None, supplied: Any = None) -> list[str]:
    payload = lexicon or load_ui_lexicon()
    panels = payload.get("panels") if isinstance(payload, dict) else {}
    spec = panels.get(panel_type, {}) if isinstance(panels, dict) else {}
    aliases = [str(item) for item in spec.get("aliases", []) if isinstance(item, str)] if isinstance(spec, dict) else []
    if isinstance(supplied, list):
        aliases.extend(str(item) for item in supplied if isinstance(item, str))
    if not aliases:
        aliases.append(panel_type)
    return unique_texts(aliases)


def default_panel_title(panel_type: str, lexicon: dict[str, Any] | None = None) -> str:
    payload = lexicon or load_ui_lexicon()
    panels = payload.get("panels") if isinstance(payload, dict) else {}
    spec = panels.get(panel_type, {}) if isinstance(panels, dict) else {}
    if isinstance(spec, dict) and str(spec.get("title") or "").strip():
        return str(spec["title"])
    return panel_type


def has_ui_operation_signal(query: Any) -> bool:
    lexicon = load_ui_lexicon()
    analyzer = DefaultMorphAnalyzer()
    for clause in analyzer.clauses(query):
        action_matches = value_matches(clause, lexicon.get("actions", {}), allow_short_fuzzy=True)
        size_matches = value_matches(clause, lexicon.get("sizes", {}), allow_short_fuzzy=True)
        position_matches = value_matches(clause, lexicon.get("positions", {}), allow_short_fuzzy=False)
        surface_matches = term_matches(clause, lexicon.get("surfaceNouns", []), allow_short_fuzzy=True)
        multi_matches = term_matches(clause, lexicon.get("multiPanelHints", []), allow_short_fuzzy=False)
        if has_layout_operation(action_matches, size_matches, position_matches, surface_matches, multi_matches):
            return True
    return False


def infer_action_size_and_position_from_query(query: Any) -> tuple[str, str | None, str | None]:
    lexicon = load_ui_lexicon()
    analyzer = DefaultMorphAnalyzer()
    for clause in analyzer.clauses(query):
        action_matches = value_matches(clause, lexicon.get("actions", {}), allow_short_fuzzy=True)
        size = best_size_match(value_matches(clause, lexicon.get("sizes", {}), allow_short_fuzzy=True))
        position = best_match(value_matches(clause, lexicon.get("positions", {}), allow_short_fuzzy=False))
        strong_action = best_match([match for match in action_matches if match.strength == "strong"])
        weak_action = best_match([match for match in action_matches if match.strength != "strong"])
        action = infer_action(strong_action, weak_action, size, position, multi_panel=False)
        if action:
            return action, size.value if size else None, position.value if position else None
    return "unknown", None, None


def load_ui_lexicon() -> dict[str, Any]:
    path = os.getenv("AGENT_UI_LEXICON_PATH")
    resolved = str(Path(path).expanduser()) if path else str(DEFAULT_UI_LEXICON_PATH)
    return _load_ui_lexicon(resolved)


@lru_cache(maxsize=8)
def _load_ui_lexicon(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def aliases_from_spec(spec: Any) -> list[str]:
    if isinstance(spec, dict):
        return [str(item) for item in spec.get("aliases", []) if isinstance(item, str)]
    if isinstance(spec, list):
        return [str(item) for item in spec if isinstance(item, str)]
    return []


def best_match(matches: Iterable[AliasMatch | None]) -> AliasMatch | None:
    real_matches = [match for match in matches if match is not None]
    if not real_matches:
        return None
    return sorted(real_matches, key=lambda item: (-item.score, item.token_index, item.value))[0]


def best_size_match(matches: Iterable[AliasMatch | None]) -> AliasMatch | None:
    real_matches = [match for match in matches if match is not None]
    if not real_matches:
        return None
    priority = {"max": 3, "large": 2, "small": 1, "min": 1}
    return sorted(real_matches, key=lambda item: (-item.score, -priority.get(item.value, 0), item.token_index, item.value))[0]


def best_panel_matches(matches: list[AliasMatch]) -> list[AliasMatch]:
    best: dict[str, AliasMatch] = {}
    for match in matches:
        current = best.get(match.value)
        if current is None or panel_match_rank(match) > panel_match_rank(current):
            best[match.value] = match
    return sorted(best.values(), key=lambda item: (-item.score, item.token_index, item.value))


def panel_match_rank(match: AliasMatch) -> tuple[float, int, int]:
    return (match.score, 1 if match.source.startswith("panel:") else 0, -match.token_index)


def ui_task_confidence(matches: Iterable[AliasMatch | None]) -> float:
    scores = [match.score for match in matches if match is not None]
    if not scores:
        return 0.0
    return min(0.95, max(0.66, sum(scores) / len(scores)))


def max_match_score(matches: Iterable[AliasMatch | None]) -> float:
    return max((match.score for match in matches if match is not None), default=0.0)


def dedupe_ui_tasks(tasks: list[UiTask]) -> list[UiTask]:
    best: dict[tuple[str, str, str, tuple[str, ...], str], UiTask] = {}
    for task in tasks:
        key = (
            task.action,
            task.targetPanelType or "",
            task.targetPanelId or "",
            tuple(task.targetPanelTypes),
            task.layoutPreset or "",
        )
        current = best.get(key)
        if current is None or task.confidence > current.confidence:
            best[key] = task
    return list(best.values())[:3]


def unique_panel_types(panel_types: Iterable[str]) -> list[str]:
    selected = {panel_type for panel_type in panel_types if panel_type in UI_PANEL_TYPES}
    return [panel_type for panel_type in UI_PANEL_TYPES if panel_type in selected]


def unique_texts(values: Iterable[str]) -> list[str]:
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result

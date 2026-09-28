"""检测阈值册：规则按生效日管理，结论按判断时点取规。

- 历史放行只承认放行事件快照的 ``rule_version``，事后改阈值不会翻案；
- 仍在处理（尚无放行结论）的批次按最新生效规则重新判断；
- 复检不覆盖旧值，每次都是一条新的 ``OBSERVATION_RECORDED``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from .timeutil import parse


@dataclass(frozen=True)
class Rule:
    rule_version: str
    metric: str
    stage: str
    effective_from: date
    limits: dict[str, float]
    unit: str | None = None

    def evaluate(self, value: float) -> str:
        if "min" in self.limits and value < self.limits["min"]:
            return "OUT"
        if "max" in self.limits and value > self.limits["max"]:
            return "OUT"
        return "IN"


@dataclass(frozen=True)
class RuleBook:
    rules: list[Rule]
    evidence_plan: Mapping[str, Any]


def load_rule_book(raw: Mapping[str, Any]) -> RuleBook:
    rules = [
        Rule(rule_version=item["rule_version"], metric=item["metric"], stage=item["stage"],
             effective_from=date.fromisoformat(item["effective_from"]),
             limits=dict(item.get("limits", {})), unit=item.get("unit"))
        for item in raw.get("rules", [])
    ]
    return RuleBook(rules=rules, evidence_plan=raw.get("evidence_plan", {}))


def rule_at(book: RuleBook, metric: str, stage: str, when: datetime) -> Rule | None:
    """取 ``when`` 时点（含当日）已生效的最新版本规则。"""
    candidates = [rule for rule in book.rules
                  if rule.metric == metric and rule.stage == stage
                  and rule.effective_from <= when.date()]
    return max(candidates, key=lambda rule: rule.effective_from, default=None)


def current_rule(book: RuleBook, metric: str, stage: str) -> Rule | None:
    rules = [rule for rule in book.rules if rule.metric == metric and rule.stage == stage]
    return max(rules, key=lambda rule: rule.effective_from, default=None)


def observation_value(observation: Mapping[str, Any]) -> float | None:
    result = observation["payload"].get("result", {})
    value = result.get("value")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def observation_metric(observation: Mapping[str, Any]) -> str:
    return observation["payload"].get("result", {}).get("metric", "")


def observation_stage(observation: Mapping[str, Any]) -> str:
    return observation["payload"].get("check_stage", "")


def evaluate_at(book: RuleBook, observation: Mapping[str, Any], when: datetime) -> str | None:
    """按指定时点的生效规则判定；无适用规则时返回 None（证据缺口）。"""
    value = observation_value(observation)
    if value is None:
        return None
    rule = rule_at(book, observation_metric(observation), observation_stage(observation), when)
    return rule.evaluate(value) if rule else None


def reevaluate_with_current(book: RuleBook, observation: Mapping[str, Any]) -> tuple[str | None, Rule | None]:
    """用最新阈值重判仍在处理的观察。"""
    value = observation_value(observation)
    if value is None:
        return None, None
    rule = current_rule(book, observation_metric(observation), observation_stage(observation))
    return (rule.evaluate(value) if rule else None), rule

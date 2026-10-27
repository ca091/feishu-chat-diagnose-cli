from __future__ import annotations

from .models import Diagnosis


def render_report(diagnosis: Diagnosis, *, analysis_kind: str = "bug") -> str:
    conclusion = "最可能根因" if analysis_kind == "bug" else "代码分析结论"
    direction = "修复方向" if analysis_kind == "bug" else "实现方向"
    sections = [
        "# 飞书项目问题诊断",
        "## 问题摘要\n" + diagnosis.problem_summary,
        "## 使用的 Skill\n" + _skills(diagnosis.skills_used),
        "## 已确认事实\n" + _list(diagnosis.facts),
        f"## {conclusion}\n" + diagnosis.root_cause + f"\n\n置信度：{diagnosis.confidence}/100",
        "## 代码证据\n" + _evidence(diagnosis),
        "## 其他假设\n" + _list(diagnosis.alternative_hypotheses),
        "## 缺失信息\n" + _list(diagnosis.missing_information),
        f"## {direction}\n" + _list(diagnosis.recommended_fix_direction),
        "## 验证建议\n" + _list(diagnosis.recommended_verification),
    ]
    return "\n\n".join(sections) + "\n"


def _list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 无"


def _inline(value: str) -> str:
    return value.replace("`", "\\`")


def _skills(items: list[str]) -> str:
    return "\n".join(f"- `{_inline(item)}`" for item in items) if items else "- 无"


def _evidence(diagnosis: Diagnosis) -> str:
    if not diagnosis.code_evidence:
        return "- 无"
    lines = []
    for item in diagnosis.code_evidence:
        location = _inline(item.path) + (f":{item.line}" if item.line is not None else "")
        symbol = f" `{_inline(item.symbol)}`" if item.symbol else ""
        lines.append(f"- `{location}`{symbol}：{item.explanation}")
    return "\n".join(lines)

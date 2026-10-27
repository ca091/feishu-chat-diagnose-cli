from feishu_chat_diagnose.models import CodeEvidence, Diagnosis
from feishu_chat_diagnose.report import render_report


def test_report_contains_evidence_confidence_and_missing_information() -> None:
    diagnosis = Diagnosis(
        problem_summary="Export shifts text", facts=["Reproduced in video"], root_cause="Double transform",
        confidence=82, code_evidence=[CodeEvidence(path="src/export.ts", symbol="exportCanvas", line=42,
                                                   explanation="Transform applied twice")],
        alternative_hypotheses=[], missing_information=["Browser version"],
        recommended_fix_direction=["Normalize transform"], recommended_verification=["Regression test"],
        skills_used=["project-bugfix"],
    )
    report = render_report(diagnosis)
    assert "## 最可能根因" in report
    assert "置信度：82/100" in report
    assert "`src/export.ts:42` `exportCanvas`" in report
    assert "## 缺失信息" in report
    assert "## 使用的 Skill\n- `project-bugfix`" in report

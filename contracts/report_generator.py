#!/usr/bin/env python3
"""ReportGenerator for TRP1 Week 8: Data Contract Enforcer.

Aggregates validation reports, violation logs, schema evolution outputs, and AI metrics
into a machine-generated Enforcer Report JSON.

Enhancements over the earlier version:
- recommendations reference concrete contract file paths and exact checks/fields
- narratives are driven directly by validation failures, violation log entries, schema report, and AI metrics
- supports contract-scoped violation logs such as violation_log/<contract_id>_violations.jsonl
- keeps report output deterministic and explainable
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FAIL_DEDUCTIONS = {
    "CRITICAL": 20,
    "HIGH": 10,
    "MEDIUM": 5,
    "LOW": 1,
    "WARNING": 0,
}

AI_DEDUCTIONS = {
    "FAIL": 20,
    "WARN": 10,
    "PASS": 0,
    "UNKNOWN": 0,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate machine-readable Enforcer Report data")

    parser.add_argument(
        "--reports-dir",
        default=None,
        help="Directory containing validation report JSON files. Prefer explicit report paths when possible.",
    )
    parser.add_argument(
        "--baseline-report",
        default=None,
        help="Explicit path to baseline validation report JSON",
    )
    parser.add_argument(
        "--violated-report",
        default=None,
        help="Explicit path to violated validation report JSON",
    )
    parser.add_argument(
        "--contract-id",
        default=None,
        help="Optional contract_id filter when loading reports from a directory",
    )
    parser.add_argument(
        "--violations",
        default=None,
        help="Path to attributed violations JSONL. If omitted, attempts violation_log/<contract_id>_violations.jsonl before fallback.",
    )
    parser.add_argument(
        "--schema-evolution",
        default=None,
        help="Optional path to schema evolution report JSON",
    )
    parser.add_argument(
        "--ai-metrics",
        default=None,
        help="Optional path to AI extensions / AI metrics JSON",
    )
    parser.add_argument(
        "--output",
        default="enforcer_report/report_data.json",
        help="Output report JSON path",
    )
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path} line {line_no}: {exc}") from exc
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def is_validation_report(payload: dict[str, Any]) -> bool:
    return "results" in payload and "contract_id" in payload


def load_validation_report(path: str | Path) -> dict[str, Any]:
    payload = load_json(path)
    if not is_validation_report(payload):
        raise ValueError(f"Not a validation report: {path}")
    payload["__path"] = str(path)
    return payload


def load_validation_reports(
    reports_dir: str | Path | None,
    contract_id: str | None = None,
) -> list[dict[str, Any]]:
    if reports_dir is None:
        return []

    report_paths = sorted(glob.glob(str(Path(reports_dir) / "*.json")))
    reports: list[dict[str, Any]] = []

    for path in report_paths:
        payload = load_json(path)
        if not is_validation_report(payload):
            continue
        if contract_id and str(payload.get("contract_id")) != contract_id:
            continue
        payload["__path"] = path
        reports.append(payload)

    return reports


def choose_reports(args: argparse.Namespace) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []

    if args.baseline_report:
        reports.append(load_validation_report(args.baseline_report))
    if args.violated_report:
        reports.append(load_validation_report(args.violated_report))

    if reports:
        if args.contract_id:
            reports = [r for r in reports if str(r.get("contract_id")) == args.contract_id]
        return reports

    return load_validation_reports(args.reports_dir, contract_id=args.contract_id)


def resolve_violation_log_path(args: argparse.Namespace, reports: list[dict[str, Any]]) -> Path:
    if args.violations:
        return Path(args.violations)

    contract_id = args.contract_id
    if not contract_id and reports:
        contract_id = str(reports[0].get("contract_id") or "")

    if contract_id:
        candidate = Path("violation_log") / f"{contract_id}_violations.jsonl"
        if candidate.exists():
            return candidate

    return Path("violation_log/violations.jsonl")


def flatten_failures(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for report in reports:
        for result in report.get("results", []):
            status = result.get("status")
            if status in {"FAIL", "ERROR"}:
                failure = dict(result)
                failure["contract_id"] = report.get("contract_id")
                failure["report_path"] = report.get("__path")
                failures.append(failure)
    return failures


def severity_counts(failures: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(item.get("severity", "LOW")) for item in failures)
    return {sev: counts.get(sev, 0) for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "WARNING"]}


def status_counts(reports: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "passed": sum(int(report.get("passed", 0)) for report in reports),
        "failed": sum(int(report.get("failed", 0)) for report in reports),
        "warned": sum(int(report.get("warned", 0)) for report in reports),
        "errored": sum(int(report.get("errored", 0)) for report in reports),
        "total_checks": sum(int(report.get("total_checks", 0)) for report in reports),
    }


def summarize_schema_changes(schema_report: dict[str, Any] | None) -> dict[str, Any]:
    if not schema_report:
        return {
            "compatibility_verdict": "UNKNOWN",
            "change_count": 0,
            "summary": [],
        }

    changes = schema_report.get("changes", []) or []
    summary = []
    for change in changes:
        summary.append(
            {
                "field_name": change.get("field_name"),
                "verdict": change.get("verdict"),
                "severity": change.get("severity"),
                "rationale": change.get("rationale"),
                "consumer_failure_modes": change.get("consumer_failure_modes", []),
            }
        )

    return {
        "compatibility_verdict": schema_report.get("compatibility_verdict", "UNKNOWN"),
        "change_count": len(changes),
        "critical_change_count": schema_report.get("critical_change_count", 0),
        "summary": summary,
        "migration_checklist": schema_report.get("migration_checklist", []),
        "rollback_plan": schema_report.get("rollback_plan", []),
    }


def summarize_ai_risk(ai_metrics: dict[str, Any] | None) -> dict[str, Any]:
    if not ai_metrics:
        return {
            "status": "UNKNOWN",
            "embedding_drift": None,
            "output_schema_violation_rate": None,
            "prompt_input_validation": None,
            "narrative": "AI-specific contract metrics were not provided for this report run.",
        }

    embedding = ai_metrics.get("embedding_drift") or ai_metrics.get("embedding") or ai_metrics.get("embedding_drift_check")
    output_rate = ai_metrics.get("output_schema") or ai_metrics.get("output_schema_violation_rate") or ai_metrics.get("llm_output")
    prompt_input = ai_metrics.get("prompt_input") or ai_metrics.get("prompt_input_validation")

    statuses = [
        str((embedding or {}).get("status", "")),
        str((output_rate or {}).get("status", "")),
        str((prompt_input or {}).get("status", "")),
    ]

    if any(status == "FAIL" for status in statuses):
        overall = "FAIL"
    elif any(status == "WARN" for status in statuses):
        overall = "WARN"
    elif any(status == "ERROR" for status in statuses):
        overall = "FAIL"
    elif any(status in {"PASS", "BASELINE_SET"} for status in statuses):
        overall = "PASS"
    else:
        overall = "UNKNOWN"

    narrative_parts: list[str] = []
    if embedding:
        narrative_parts.append(
            f"Embedding drift status is {embedding.get('status')} with score {embedding.get('drift_score')}."
        )
    if output_rate:
        narrative_parts.append(
            f"Structured output schema status is {output_rate.get('status')} with violation rate {output_rate.get('violation_rate')}."
        )
    if prompt_input:
        narrative_parts.append(
            f"Prompt input validation status is {prompt_input.get('status')}"
            + (
                f" with quarantined count {prompt_input.get('quarantined')}."
                if prompt_input.get("quarantined") is not None
                else "."
            )
        )

    if not narrative_parts:
        narrative_parts.append("AI-specific metrics were provided in a format that could not be fully summarized.")

    return {
        "status": overall,
        "embedding_drift": embedding,
        "output_schema_violation_rate": output_rate,
        "prompt_input_validation": prompt_input,
        "narrative": " ".join(narrative_parts),
    }


def compute_health_score(
    reports: list[dict[str, Any]],
    ai_summary: dict[str, Any],
    schema_summary: dict[str, Any],
    violations: list[dict[str, Any]],
) -> int:
    if not reports:
        return 0

    totals = status_counts(reports)
    total_checks = totals["total_checks"]
    passed_checks = totals["passed"]

    if total_checks <= 0:
        return 0

    base_score = round((passed_checks / total_checks) * 100)

    failures = flatten_failures(reports)
    validation_deduction = sum(
        FAIL_DEDUCTIONS.get(str(item.get("severity", "LOW")), 1) for item in failures
    )

    ai_deduction = AI_DEDUCTIONS.get(str(ai_summary.get("status", "UNKNOWN")), 0)

    schema_verdict = str(schema_summary.get("compatibility_verdict", "UNKNOWN"))
    if schema_verdict == "BREAKING":
        schema_deduction = 15
    elif schema_verdict in {"COMPATIBLE", "NO_CHANGE"}:
        schema_deduction = 0
    else:
        schema_deduction = 5

    violation_deduction = min(len(violations) * 3, 15)

    score = base_score - validation_deduction - ai_deduction - schema_deduction - violation_deduction
    return max(0, min(100, score))


def contract_yaml_path(contract_id: str) -> str:
    safe_name = contract_id.replace("-", "_")
    return f"generated_contracts/{safe_name}.yaml"


def contract_clause_reference(check_id: str, column: str) -> str:
    return f"check '{check_id}' on field '{column}'"


def top_violations(failures: list[dict[str, Any]], n: int = 3) -> list[dict[str, Any]]:
    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        sev = str(item.get("severity", "LOW"))
        sev_rank = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "WARNING": 1}.get(sev, 0)
        records = int(item.get("records_failing", 0) or 0)
        return (sev_rank, records)

    ranked = sorted(failures, key=sort_key, reverse=True)
    return ranked[:n]


def format_violation_description(item: dict[str, Any]) -> str:
    contract_id = str(item.get("contract_id", "unknown-system"))
    column = str(item.get("column_name", item.get("field_name", "unknown_field")))
    check_id = str(item.get("check_id", item.get("check_type", "unknown_check")))
    expected = str(item.get("expected", item.get("warn_threshold", "expected contract condition")))
    actual = str(item.get("actual_value", item.get("metric_value", "unknown actual value")))
    records = item.get("records_failing", item.get("schema_violations", "unknown"))
    return (
        f"Contract {contract_id} violated {contract_clause_reference(check_id, column)}. "
        f"Expected '{expected}', observed '{actual}', affecting {records} records/outputs. "
        f"Review {contract_yaml_path(contract_id)} before the next run."
    )


def merge_failure_and_violation_signals(
    failures: list[dict[str, Any]],
    violations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = list(failures)

    for violation in violations:
        merged.append(
            {
                "contract_id": violation.get("contract_id"),
                "column_name": violation.get("column_name") or violation.get("field_name") or violation.get("metric_name"),
                "check_id": violation.get("check_id"),
                "severity": violation.get("severity", "WARN"),
                "expected": violation.get("expected") or violation.get("warn_threshold"),
                "actual_value": violation.get("actual_value") or violation.get("metric_value"),
                "records_failing": violation.get("records_failing") or violation.get("schema_violations") or 1,
                "report_path": violation.get("source"),
            }
        )

    return merged


def health_narrative(
    score: int,
    failures: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    ai_summary: dict[str, Any],
    schema_summary: dict[str, Any],
) -> str:
    critical_count = sum(1 for f in failures if f.get("severity") == "CRITICAL")
    ai_status = str(ai_summary.get("status", "UNKNOWN"))
    schema_verdict = str(schema_summary.get("compatibility_verdict", "UNKNOWN"))
    violation_count = len(violations)

    if score >= 90 and violation_count == 0 and ai_status in {"PASS", "UNKNOWN"}:
        return f"Data health score is {score}/100. No significant validation, schema, or AI issues were observed in the supplied artifacts."

    if ai_status == "FAIL":
        return (
            f"Data health score is {score}/100. AI-specific risk controls are failing, and the violation log indicates "
            f"{violation_count} contract issues requiring immediate remediation before deployment."
        )

    if critical_count > 0:
        return (
            f"Data health score is {score}/100. There are {critical_count} critical validation failures and "
            f"{violation_count} logged contract violations requiring immediate engineering action."
        )

    if schema_verdict == "BREAKING":
        return (
            f"Data health score is {score}/100. Schema evolution is marked BREAKING; deployment should wait until the "
            f"migration checklist has been completed for impacted subscribers."
        )

    return (
        f"Data health score is {score}/100. The system remains operational, but validation, schema, or AI contract "
        f"signals in the supplied artifacts indicate remediation is needed before production rollout."
    )


def derive_recommendations(
    failures: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    schema_summary: dict[str, Any],
    ai_summary: dict[str, Any],
) -> list[str]:
    recommendations: list[str] = []

    ranked = top_violations(merge_failure_and_violation_signals(failures, violations), n=5)

    for item in ranked:
        contract_id = str(item.get("contract_id", "unknown-system"))
        contract_path = contract_yaml_path(contract_id)
        column = str(item.get("column_name", "unknown_field"))
        check_id = str(item.get("check_id", "unknown_check"))
        expected = str(item.get("expected", "expected contract condition"))
        actual = str(item.get("actual_value", "unknown actual value"))

        recommendations.append(
            f"Update {contract_path} and the upstream producer so {contract_clause_reference(check_id, column)} "
            f"matches expected='{expected}' instead of actual='{actual}' for contract_id={contract_id}."
        )
        if len(recommendations) >= 3:
            break

    if len(recommendations) < 3 and schema_summary.get("compatibility_verdict") == "BREAKING":
        checklist = schema_summary.get("migration_checklist", [])
        if checklist:
            recommendations.append(str(checklist[0]))
        else:
            recommendations.append(
                "Review validation_reports/schema_evolution_*.json and execute the subscriber-specific migration checklist before deployment."
            )

    if len(recommendations) < 3 and ai_summary.get("status") in {"WARN", "FAIL"}:
        output_schema = ai_summary.get("output_schema_violation_rate") or {}
        rate = output_schema.get("violation_rate", "unknown")
        threshold = output_schema.get("warn_threshold", "unknown")
        recommendations.append(
            f"Inspect violation_log/*_violations.jsonl and reduce output schema violation rate from {rate} to below {threshold} before rebaselining AI checks."
        )

    while len(recommendations) < 3:
        recommendations.append(
            "Regenerate generated_contracts/*.yaml and rerun validation_reports/*.json after each upstream schema change so contract clauses and enforcement outputs remain synchronized."
        )

    return recommendations[:3]


def summarize_attributed_violations(violations: list[dict[str, Any]]) -> dict[str, Any]:
    if not violations:
        return {
            "violation_count": 0,
            "top_blame_candidates": [],
        }

    top_candidates: list[dict[str, Any]] = []
    for violation in violations[:3]:
        blame_chain = violation.get("blame_chain", []) or []
        if blame_chain:
            top_candidates.append(
                {
                    "check_id": violation.get("check_id"),
                    "top_candidate": blame_chain[0],
                    "blast_radius": violation.get("blast_radius", {}),
                }
            )

    return {
        "violation_count": len(violations),
        "top_blame_candidates": top_candidates,
    }


def build_report(
    reports: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    schema_report: dict[str, Any] | None,
    ai_metrics: dict[str, Any] | None,
    violation_log_path: Path,
) -> dict[str, Any]:
    failures = flatten_failures(reports)
    schema_summary = summarize_schema_changes(schema_report)
    ai_summary = summarize_ai_risk(ai_metrics)
    score = compute_health_score(reports, ai_summary, schema_summary, violations)
    recommendations = derive_recommendations(failures, violations, schema_summary, ai_summary)

    now = datetime.now(timezone.utc)
    period_start = (now - timedelta(days=7)).date().isoformat()
    period_end = now.date().isoformat()

    merged_top_items = top_violations(merge_failure_and_violation_signals(failures, violations), n=3)

    return {
        "generated_at": iso_now(),
        "period": f"{period_start} to {period_end}",
        "data_health_score": score,
        "health_narrative": health_narrative(score, failures, violations, ai_summary, schema_summary),
        "validation_summary": status_counts(reports),
        "violations_this_week": {
            "count_by_severity": severity_counts(merge_failure_and_violation_signals(failures, violations)),
            "top_violations": [format_violation_description(item) for item in merged_top_items],
        },
        "schema_changes_detected": schema_summary,
        "ai_system_risk_assessment": ai_summary,
        "recommended_actions": recommendations,
        "attribution_summary": summarize_attributed_violations(violations),
        "source_artifacts": {
            "validation_report_count": len(reports),
            "attributed_violation_count": len(violations),
            "schema_evolution_included": schema_report is not None,
            "ai_metrics_included": ai_metrics is not None,
            "report_paths": [report.get("__path") for report in reports],
            "violation_log_path": str(violation_log_path),
            "contract_ids": sorted({str(report.get("contract_id")) for report in reports}),
        },
    }


def main() -> None:
    args = parse_args()

    reports = choose_reports(args)
    violation_log_path = resolve_violation_log_path(args, reports)
    violations = load_jsonl(violation_log_path)
    schema_report = load_json(args.schema_evolution) if args.schema_evolution and Path(args.schema_evolution).exists() else None
    ai_metrics = load_json(args.ai_metrics) if args.ai_metrics and Path(args.ai_metrics).exists() else None

    report = build_report(reports, violations, schema_report, ai_metrics, violation_log_path)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"[OK] validation_reports={len(reports)}")
    print(f"[OK] attributed_violations={len(violations)}")
    print(f"[OK] violation_log={violation_log_path}")
    print(f"[OK] output={output_path}")
    print(f"[OK] data_health_score={report['data_health_score']}")

    ai = report.get("ai_system_risk_assessment", {})
    if ai:
        print(f"[OK] ai_status={ai.get('status')}")

        emb = ai.get("embedding_drift")
        prompt = ai.get("prompt_input_validation")
        output_schema = ai.get("output_schema_violation_rate")

        emb_status = emb.get("status") if isinstance(emb, dict) else None
        prompt_status = prompt.get("status") if isinstance(prompt, dict) else None
        output_status = output_schema.get("status") if isinstance(output_schema, dict) else None

        print(
            f"[OK] ai_breakdown="
            f"embedding={emb_status} "
            f"prompt={prompt_status} "
            f"output_schema={output_status}"
        )


if __name__ == "__main__":
    main()

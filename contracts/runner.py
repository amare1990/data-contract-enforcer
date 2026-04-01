#!/usr/bin/env python3
"""ValidationRunner for TRP1 Week 8: Data Contract Enforcer.

Runs contract checks against a JSONL dataset and emits a structured JSON report.

Example:
    uv run python contracts/runner.py \
      --contract generated_contracts/week3_extractions.yaml \
      --data outputs/week3/extractions.jsonl \
      --output validation_reports/week3_baseline.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
ISO_8601_Z_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run contract validation against JSONL data")
    parser.add_argument("--contract", required=True, help="Path to contract YAML")
    parser.add_argument("--data", required=True, help="Path to JSONL data file")
    parser.add_argument("--output", required=True, help="Path to validation report JSON")
    return parser.parse_args()


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"JSONL source not found: {path}")

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no} of {path}: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"Expected JSON object on line {line_no} of {path}")
            records.append(obj)

    if not records:
        raise ValueError(f"No JSON objects found in {path}")
    return records


def load_contract(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sha256_of_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def to_iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_string(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def normalize_scalar(value: Any) -> Any:
    if isinstance(value, list):
        return None
    if isinstance(value, dict):
        return None
    return value


def get_column_values(records: list[dict[str, Any]], column_name: str) -> list[Any]:
    values: list[Any] = []
    for record in records:
        values.append(record.get(column_name))
    return values


def try_parse_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        if math.isfinite(parsed):
            return parsed
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        if math.isfinite(parsed):
            return parsed
    return None


def is_numeric_column(values: list[Any]) -> bool:
    non_null = [v for v in values if v is not None]
    if not non_null:
        return False
    numeric_count = sum(1 for v in non_null if try_parse_float(v) is not None)
    return (numeric_count / len(non_null)) >= 0.95


def collect_numeric_values(values: list[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        parsed = try_parse_float(value)
        if parsed is not None:
            out.append(parsed)
    return out


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    variance = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(max(variance, 0.0))


def percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("Cannot compute percentile of empty list")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def build_result(
    check_id: str,
    column_name: str,
    check_type: str,
    status: str,
    actual_value: str,
    expected: str,
    severity: str,
    records_failing: int = 0,
    sample_failing: list[str] | None = None,
    message: str = "",
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "column_name": column_name,
        "check_type": check_type,
        "status": status,
        "actual_value": actual_value,
        "expected": expected,
        "severity": severity,
        "records_failing": records_failing,
        "sample_failing": sample_failing or [],
        "message": message,
    }


def severity_for_status(status: str, default_fail_severity: str = "CRITICAL") -> str:
    if status == "FAIL":
        return default_fail_severity
    if status == "WARN":
        return "WARNING"
    if status == "ERROR":
        return "CRITICAL"
    return "LOW"


def record_identifier(record: dict[str, Any], fallback_index: int) -> str:
    for key in ("fact_id", "ldu_id", "event_id", "doc_id", "id"):
        if key in record and record[key] is not None:
            return safe_string(record[key])
    return f"record_{fallback_index}"


def check_required(column: str, values: list[Any], contract_id: str) -> dict[str, Any]:
    failing = [i for i, v in enumerate(values) if v is None or (isinstance(v, str) and not v.strip())]
    status = "PASS" if not failing else "FAIL"
    return build_result(
        check_id=f"{contract_id}.{column}.required",
        column_name=column,
        check_type="required",
        status=status,
        actual_value=f"missing_count={len(failing)}",
        expected="missing_count=0",
        severity=severity_for_status(status),
        records_failing=len(failing),
        sample_failing=[str(i) for i in failing[:5]],
        message="Required field must be present on every record." if failing else "Required field present on all records.",
    )


def check_type(column: str, values: list[Any], expected_type: str, contract_id: str) -> dict[str, Any]:
    non_null = [v for v in values if v is not None]
    if not non_null:
        return build_result(
            check_id=f"{contract_id}.{column}.type",
            column_name=column,
            check_type="type",
            status="ERROR",
            actual_value="column missing or all null",
            expected=f"type={expected_type}",
            severity="CRITICAL",
            records_failing=0,
            message="Column cannot be type-checked because no non-null values were found.",
        )

    failing: list[int] = []
    if expected_type in {"number", "integer"}:
        for i, v in enumerate(values):
            if v is None:
                continue
            parsed = try_parse_float(v)
            if parsed is None:
                failing.append(i)
            elif expected_type == "integer" and not float(parsed).is_integer():
                failing.append(i)
    elif expected_type == "string":
        for i, v in enumerate(values):
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                failing.append(i)
    elif expected_type == "boolean":
        for i, v in enumerate(values):
            if v is None:
                continue
            if not isinstance(v, bool):
                failing.append(i)

    status = "PASS" if not failing else "FAIL"
    return build_result(
        check_id=f"{contract_id}.{column}.type",
        column_name=column,
        check_type="type",
        status=status,
        actual_value=f"invalid_type_count={len(failing)}",
        expected=f"type={expected_type}",
        severity=severity_for_status(status),
        records_failing=len(failing),
        sample_failing=[str(i) for i in failing[:5]],
        message=(
            f"Observed values do not conform to expected type {expected_type}."
            if failing
            else f"All non-null values conform to expected type {expected_type}."
        ),
    )


def check_enum(column: str, values: list[Any], enum_values: list[str], contract_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    allowed = set(enum_values)
    failing_indices: list[int] = []
    bad_values: list[str] = []
    for i, v in enumerate(values):
        if v is None:
            continue
        sv = safe_string(v)
        if sv not in allowed:
            failing_indices.append(i)
            bad_values.append(sv)

    status = "PASS" if not failing_indices else "FAIL"
    return build_result(
        check_id=f"{contract_id}.{column}.enum",
        column_name=column,
        check_type="enum",
        status=status,
        actual_value=(
            "all_values_within_enum"
            if not failing_indices
            else f"invalid_count={len(failing_indices)}, invalid_values={sorted(set(bad_values))[:5]}"
        ),
        expected=f"values in {enum_values}",
        severity=severity_for_status(status),
        records_failing=len(failing_indices),
        sample_failing=[record_identifier(records[i], i) for i in failing_indices[:5]],
        message="Enum conformance check." if not failing_indices else "Observed values outside allowed enum.",
    )


def check_uuid_format(column: str, values: list[Any], contract_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    failing: list[int] = []
    for i, v in enumerate(values):
        if v is None:
            continue
        if not UUID_RE.match(safe_string(v).strip()):
            failing.append(i)

    status = "PASS" if not failing else "FAIL"
    return build_result(
        check_id=f"{contract_id}.{column}.uuid_format",
        column_name=column,
        check_type="uuid_format",
        status=status,
        actual_value=f"invalid_uuid_count={len(failing)}",
        expected="regex ^[0-9a-fA-F-]{36}$",
        severity=severity_for_status(status),
        records_failing=len(failing),
        sample_failing=[record_identifier(records[i], i) for i in failing[:5]],
        message="UUID format validation." if not failing else "One or more values do not match UUID format.",
    )


def check_datetime_format(column: str, values: list[Any], contract_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    failing: list[int] = []
    for i, v in enumerate(values):
        if v is None:
            continue
        s = safe_string(v).strip()
        if not ISO_8601_Z_RE.match(s):
            failing.append(i)
            continue
        try:
            datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            failing.append(i)

    status = "PASS" if not failing else "FAIL"
    return build_result(
        check_id=f"{contract_id}.{column}.datetime_format",
        column_name=column,
        check_type="datetime_format",
        status=status,
        actual_value=f"invalid_datetime_count={len(failing)}",
        expected="ISO 8601 date-time",
        severity=severity_for_status(status),
        records_failing=len(failing),
        sample_failing=[record_identifier(records[i], i) for i in failing[:5]],
        message="Date-time format validation." if not failing else "One or more values are not parseable ISO 8601 timestamps.",
    )


def check_range(column: str, values: list[Any], minimum: float | None, maximum: float | None, contract_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    numeric_values = collect_numeric_values(values)
    if not numeric_values:
        return build_result(
            check_id=f"{contract_id}.{column}.range",
            column_name=column,
            check_type="range",
            status="ERROR",
            actual_value="no_numeric_values_found",
            expected=f"min>={minimum}, max<={maximum}",
            severity="CRITICAL",
            message="Range check could not execute because no numeric values were available.",
        )

    failing: list[int] = []
    for i, v in enumerate(values):
        parsed = try_parse_float(v)
        if parsed is None:
            continue
        if minimum is not None and parsed < minimum:
            failing.append(i)
        elif maximum is not None and parsed > maximum:
            failing.append(i)

    actual = f"min={min(numeric_values):.4f}, max={max(numeric_values):.4f}, mean={mean(numeric_values):.4f}"
    expected_parts: list[str] = []
    if minimum is not None:
        expected_parts.append(f"min>={minimum}")
    if maximum is not None:
        expected_parts.append(f"max<={maximum}")
    expected = ", ".join(expected_parts)
    status = "PASS" if not failing else "FAIL"
    return build_result(
        check_id=f"{contract_id}.{column}.range",
        column_name=column,
        check_type="range",
        status=status,
        actual_value=actual,
        expected=expected,
        severity=severity_for_status(status),
        records_failing=len(failing),
        sample_failing=[record_identifier(records[i], i) for i in failing[:5]],
        message=(
            "Numeric values are within the allowed range."
            if not failing
            else "Observed numeric values violate the configured minimum/maximum bounds."
        ),
    )


def load_baselines(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"columns": {}}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_baselines(path: Path, columns: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"written_at": to_iso_now(), "columns": columns}
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def check_statistical_drift(column: str, values: list[Any], baselines: dict[str, Any], contract_id: str) -> dict[str, Any] | None:
    baseline_columns = baselines.get("columns", {})
    if column not in baseline_columns:
        return None

    numeric_values = collect_numeric_values(values)
    if not numeric_values:
        return build_result(
            check_id=f"{contract_id}.{column}.statistical_drift",
            column_name=column,
            check_type="statistical_drift",
            status="ERROR",
            actual_value="no_numeric_values_found",
            expected="baseline mean/stddev available",
            severity="CRITICAL",
            message="Drift check could not execute because current numeric values are unavailable.",
        )

    baseline = baseline_columns[column]
    current_mean = mean(numeric_values)
    baseline_mean = float(baseline["mean"])
    baseline_std = float(baseline["stddev"])
    z_score = abs(current_mean - baseline_mean) / max(baseline_std, 1e-9)

    if z_score > 3:
        status = "FAIL"
        severity = "HIGH"
        message = f"{column} mean drifted {z_score:.1f} stddev from baseline"
    elif z_score > 2:
        status = "WARN"
        severity = "WARNING"
        message = f"{column} mean within warning range ({z_score:.1f} stddev)"
    else:
        status = "PASS"
        severity = "LOW"
        message = f"{column} mean within baseline tolerance ({z_score:.1f} stddev)"

    return build_result(
        check_id=f"{contract_id}.{column}.statistical_drift",
        column_name=column,
        check_type="statistical_drift",
        status=status,
        actual_value=f"current_mean={current_mean:.4f}, z_score={z_score:.2f}",
        expected=f"baseline_mean={baseline_mean:.4f}, threshold<=2 WARN, <=3 FAIL",
        severity=severity,
        records_failing=0,
        sample_failing=[],
        message=message,
    )


def update_baseline_candidates(records: list[dict[str, Any]], contract_schema: dict[str, Any]) -> dict[str, Any]:
    baseline_columns: dict[str, Any] = {}
    for column in contract_schema:
        values = get_column_values(records, column)
        numeric_values = collect_numeric_values(values)
        non_null = [v for v in values if v is not None]
        if not non_null:
            continue
        if (len(numeric_values) / len(non_null)) < 0.95:
            continue
        if not numeric_values:
            continue
        baseline_columns[column] = {
            "mean": mean(numeric_values),
            "stddev": stddev(numeric_values),
        }
    return baseline_columns


def validate_contract(records: list[dict[str, Any]], contract: dict[str, Any], data_path: str | Path) -> dict[str, Any]:
    contract_id = contract.get("id", "unknown_contract")
    contract_schema = contract.get("schema", {})
    results: list[dict[str, Any]] = []

    baselines_path = Path("schema_snapshots") / "baselines.json"
    baselines = load_baselines(baselines_path)

    for column, clause in contract_schema.items():
        values = get_column_values(records, column)
        column_missing = all(v is None for v in values)

        if clause.get("required"):
            results.append(check_required(column, values, contract_id))

        if column_missing:
            results.append(
                build_result(
                    check_id=f"{contract_id}.{column}.presence",
                    column_name=column,
                    check_type="presence",
                    status="ERROR",
                    actual_value="column_missing",
                    expected="column exists",
                    severity="CRITICAL",
                    message="Column does not exist in the observed dataset; continuing with partial failure handling.",
                )
            )
            continue

        expected_type = clause.get("type")
        if expected_type:
            results.append(check_type(column, values, expected_type, contract_id))

        if "enum" in clause:
            results.append(check_enum(column, values, clause["enum"], contract_id, records))

        if clause.get("format") == "uuid":
            results.append(check_uuid_format(column, values, contract_id, records))
        elif clause.get("format") == "date-time":
            results.append(check_datetime_format(column, values, contract_id, records))

        if "minimum" in clause or "maximum" in clause:
            results.append(
                check_range(
                    column,
                    values,
                    clause.get("minimum"),
                    clause.get("maximum"),
                    contract_id,
                    records,
                )
            )

        drift_result = check_statistical_drift(column, values, baselines, contract_id)
        if drift_result is not None:
            results.append(drift_result)

    baseline_candidates = update_baseline_candidates(records, contract_schema)
    if baseline_candidates:
        write_baselines(baselines_path, baseline_candidates)

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    warned = sum(1 for r in results if r["status"] == "WARN")
    errored = sum(1 for r in results if r["status"] == "ERROR")

    return {
        "report_id": str(uuid.uuid4()),
        "contract_id": contract_id,
        "snapshot_id": sha256_of_file(data_path),
        "run_timestamp": to_iso_now(),
        "total_checks": len(results),
        "passed": passed,
        "failed": failed,
        "warned": warned,
        "errored": errored,
        "results": results,
    }


def main() -> None:
    args = parse_args()
    contract = load_contract(args.contract)
    records = load_jsonl(args.data)
    report = validate_contract(records, contract, args.data)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"[OK] contract={args.contract}")
    print(f"[OK] data={args.data}")
    print(f"[OK] output={output_path}")
    print(
        "[OK] summary="
        f"total={report['total_checks']} passed={report['passed']} failed={report['failed']} warned={report['warned']} errored={report['errored']}"
    )


if __name__ == "__main__":
    main()

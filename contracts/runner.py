#!/usr/bin/env python3
"""ValidationRunner for TRP1 Week 8: Data Contract Enforcer.

Runs contract checks against a JSONL dataset and emits a structured JSON report.

Key design goals:
- general nested-path validation for dotted fields and [*] array paths
- compatibility with generator.py path-based schema inference
- sparse optional fields should not become false column_missing errors
- support for common contract checks plus generic cross-field quality rules
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# from contracts.path_utils import extract_first, extract_values, path_exists_in_dataset
from path_utils import extract_first, extract_values, path_exists_in_dataset


UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
ISO_8601_Z_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run contract validation against JSONL data")
    parser.add_argument("--contract", required=True, help="Path to contract YAML")
    parser.add_argument("--data", required=True, help="Path to JSONL data file")
    parser.add_argument("--output", required=True, help="Path to validation report JSON")
    parser.add_argument("--mode", choices=["AUDIT", "WARN", "ENFORCE"], default="AUDIT", help="Enforcement mode (default: AUDIT)")
    parser.add_argument("--fail-on-block", action="store_true", help="Exit non-zero when the selected mode would block the pipeline")
    return parser.parse_args()




def blocking_severities_for_mode(mode: str) -> set[str]:
    normalized = mode.upper()
    if normalized == "WARN":
        return {"CRITICAL"}
    if normalized == "ENFORCE":
        return {"CRITICAL", "HIGH"}
    return set()


def compute_blocking_violation_count(results: list[dict[str, Any]], mode: str) -> int:
    blocking_severities = blocking_severities_for_mode(mode)
    if not blocking_severities:
        return 0
    count = 0
    for result in results:
        if result.get("status") not in {"FAIL", "ERROR"}:
            continue
        if str(result.get("severity", "")).upper() in blocking_severities:
            count += 1
    return count


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
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def try_parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    s = safe_string(value).strip()
    if not s or not ISO_8601_Z_RE.match(s):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def try_parse_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    variance = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(max(variance, 0.0))


def record_identifier(record: dict[str, Any], fallback_index: int) -> str:
    for key in ("fact_id", "ldu_id", "event_id", "doc_id", "document_id", "id", "snapshot_id", "trace_id"):
        if key in record and record[key] is not None:
            return safe_string(record[key])
    return f"record_{fallback_index}"


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


def dataset_has_column(records: list[dict[str, Any]], column_name: str) -> bool:
    return path_exists_in_dataset(records, column_name)


def get_column_values(records: list[dict[str, Any]], column_name: str) -> list[Any]:
    values: list[Any] = []
    for record in records:
        extracted = extract_values(record, column_name)
        if not extracted:
            values.append(None)
        else:
            values.extend(extracted)
    return values


def collect_non_null_values(records: list[dict[str, Any]], column_name: str) -> list[Any]:
    out: list[Any] = []
    for record in records:
        for value in extract_values(record, column_name):
            if value is not None:
                out.append(value)
    return out


def count_missing_required(records: list[dict[str, Any]], column_name: str) -> int:
    missing = 0
    for record in records:
        vals = extract_values(record, column_name)
        if not vals or all(v is None for v in vals):
            missing += 1
    return missing


def collect_numeric_values(values: list[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        parsed = try_parse_float(value)
        if parsed is not None:
            out.append(parsed)
    return out


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


def check_required(
    records: list[dict[str, Any]],
    column: str,
    required: bool,
    contract_id: str,
) -> dict[str, Any] | None:
    if not required:
        return None

    exists_anywhere = dataset_has_column(records, column)
    missing_count = count_missing_required(records, column)

    if not exists_anywhere:
        status = "ERROR"
        actual_value = "column_missing"
        message = "Required field path does not exist anywhere in the dataset."
    elif missing_count > 0:
        status = "FAIL"
        actual_value = f"missing_count={missing_count}"
        message = f"Required field missing on {missing_count} records."
    else:
        status = "PASS"
        actual_value = "missing_count=0"
        message = "Required field present on all records."

    return build_result(
        check_id=f"{contract_id}.{column}.required",
        column_name=column,
        check_type="required",
        status=status,
        actual_value=actual_value,
        expected="missing_count=0",
        severity=severity_for_status(status),
        records_failing=missing_count if status == "FAIL" else 0,
        message=message,
    )


def check_type(
    records: list[dict[str, Any]],
    column: str,
    expected_type: str,
    contract_id: str,
) -> dict[str, Any]:
    values = collect_non_null_values(records, column)
    exists_anywhere = dataset_has_column(records, column)

    if not values:
        if exists_anywhere:
            return build_result(
                check_id=f"{contract_id}.{column}.type",
                column_name=column,
                check_type="type",
                status="WARN",
                actual_value="no_non_null_values_found",
                expected=f"type={expected_type}",
                severity="WARNING",
                message="Field exists but no non-null values were available for type checking.",
            )
        return build_result(
            check_id=f"{contract_id}.{column}.type",
            column_name=column,
            check_type="type",
            status="ERROR",
            actual_value="column_missing",
            expected=f"type={expected_type}",
            severity="CRITICAL",
            message="Column/path does not exist in dataset.",
        )

    invalid_count = 0

    if expected_type == "integer":
        for value in values:
            parsed = try_parse_float(value)
            if parsed is None or not float(parsed).is_integer():
                invalid_count += 1
    elif expected_type == "number":
        for value in values:
            if try_parse_float(value) is None:
                invalid_count += 1
    elif expected_type == "string":
        for value in values:
            if isinstance(value, (dict, list)):
                invalid_count += 1
    elif expected_type == "boolean":
        for value in values:
            if not isinstance(value, bool):
                invalid_count += 1
    elif expected_type == "object":
        for value in values:
            if not isinstance(value, dict):
                invalid_count += 1
    elif expected_type == "array":
        for value in values:
            if not isinstance(value, list):
                invalid_count += 1

    status = "PASS" if invalid_count == 0 else "FAIL"
    return build_result(
        check_id=f"{contract_id}.{column}.type",
        column_name=column,
        check_type="type",
        status=status,
        actual_value=f"invalid_type_count={invalid_count}",
        expected=f"type={expected_type}",
        severity=severity_for_status(status),
        records_failing=invalid_count,
        message=(
            f"All non-null observed values conform to expected type {expected_type}."
            if status == "PASS"
            else f"Observed values do not conform to expected type {expected_type}."
        ),
    )


def check_enum(
    records: list[dict[str, Any]],
    column: str,
    enum_values: list[str],
    contract_id: str,
) -> dict[str, Any]:
    values = collect_non_null_values(records, column)
    exists_anywhere = dataset_has_column(records, column)

    if not values:
        if exists_anywhere:
            return build_result(
                check_id=f"{contract_id}.{column}.enum",
                column_name=column,
                check_type="enum",
                status="WARN",
                actual_value="no_non_null_values_found",
                expected=f"values in {enum_values}",
                severity="WARNING",
                message="Field exists but no non-null values were available for enum checking.",
            )
        return build_result(
            check_id=f"{contract_id}.{column}.enum",
            column_name=column,
            check_type="enum",
            status="ERROR",
            actual_value="column_missing",
            expected=f"values in {enum_values}",
            severity="CRITICAL",
            message="Column/path does not exist in dataset.",
        )

    allowed = set(enum_values)
    bad_values = sorted({safe_string(v) for v in values if safe_string(v) not in allowed})
    invalid_count = len([v for v in values if safe_string(v) not in allowed])

    status = "PASS" if invalid_count == 0 else "FAIL"
    return build_result(
        check_id=f"{contract_id}.{column}.enum",
        column_name=column,
        check_type="enum",
        status=status,
        actual_value="all_values_within_enum" if status == "PASS" else f"invalid_count={invalid_count}, invalid_values={bad_values[:5]}",
        expected=f"values in {enum_values}",
        severity=severity_for_status(status),
        records_failing=invalid_count,
        message="Enum conformance check." if status == "PASS" else "Observed values outside allowed enum.",
    )


def check_uuid_format(
    records: list[dict[str, Any]],
    column: str,
    contract_id: str,
) -> dict[str, Any]:
    values = collect_non_null_values(records, column)
    exists_anywhere = dataset_has_column(records, column)

    if not values:
        if exists_anywhere:
            return build_result(
                check_id=f"{contract_id}.{column}.uuid_format",
                column_name=column,
                check_type="uuid_format",
                status="WARN",
                actual_value="no_non_null_values_found",
                expected="regex ^[0-9a-fA-F-]{36}$",
                severity="WARNING",
                message="Field exists but no non-null values were available for UUID validation.",
            )
        return build_result(
            check_id=f"{contract_id}.{column}.uuid_format",
            column_name=column,
            check_type="uuid_format",
            status="ERROR",
            actual_value="column_missing",
            expected="regex ^[0-9a-fA-F-]{36}$",
            severity="CRITICAL",
            message="Column/path does not exist in dataset.",
        )

    invalid_count = sum(1 for v in values if not UUID_RE.match(safe_string(v).strip()))
    status = "PASS" if invalid_count == 0 else "FAIL"

    return build_result(
        check_id=f"{contract_id}.{column}.uuid_format",
        column_name=column,
        check_type="uuid_format",
        status=status,
        actual_value=f"invalid_uuid_count={invalid_count}",
        expected="regex ^[0-9a-fA-F-]{36}$",
        severity=severity_for_status(status),
        records_failing=invalid_count,
        message="UUID format validation." if status == "PASS" else "One or more values do not match UUID format.",
    )


def check_datetime_format(
    records: list[dict[str, Any]],
    column: str,
    contract_id: str,
) -> dict[str, Any]:
    values = collect_non_null_values(records, column)
    exists_anywhere = dataset_has_column(records, column)

    if not values:
        if exists_anywhere:
            return build_result(
                check_id=f"{contract_id}.{column}.datetime_format",
                column_name=column,
                check_type="datetime_format",
                status="WARN",
                actual_value="no_non_null_values_found",
                expected="ISO 8601 date-time",
                severity="WARNING",
                message="Field exists but no non-null values were available for datetime validation.",
            )
        return build_result(
            check_id=f"{contract_id}.{column}.datetime_format",
            column_name=column,
            check_type="datetime_format",
            status="ERROR",
            actual_value="column_missing",
            expected="ISO 8601 date-time",
            severity="CRITICAL",
            message="Column/path does not exist in dataset.",
        )

    invalid_count = sum(1 for v in values if try_parse_datetime(v) is None)
    status = "PASS" if invalid_count == 0 else "FAIL"

    return build_result(
        check_id=f"{contract_id}.{column}.datetime_format",
        column_name=column,
        check_type="datetime_format",
        status=status,
        actual_value=f"invalid_datetime_count={invalid_count}",
        expected="ISO 8601 date-time",
        severity=severity_for_status(status),
        records_failing=invalid_count,
        message="Date-time format validation." if status == "PASS" else "One or more values are not parseable ISO 8601 timestamps.",
    )


def check_range(
    records: list[dict[str, Any]],
    column: str,
    minimum: float | None,
    maximum: float | None,
    contract_id: str,
) -> dict[str, Any]:
    values = collect_non_null_values(records, column)
    exists_anywhere = dataset_has_column(records, column)

    if not values:
        if exists_anywhere:
            return build_result(
                check_id=f"{contract_id}.{column}.range",
                column_name=column,
                check_type="range",
                status="WARN",
                actual_value="no_non_null_values_found",
                expected=f"min>={minimum}, max<={maximum}",
                severity="WARNING",
                message="Field exists but no non-null values were available for range checking.",
            )
        return build_result(
            check_id=f"{contract_id}.{column}.range",
            column_name=column,
            check_type="range",
            status="ERROR",
            actual_value="column_missing",
            expected=f"min>={minimum}, max<={maximum}",
            severity="CRITICAL",
            message="Column/path does not exist in dataset.",
        )

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

    invalid_count = 0
    for value in numeric_values:
        if minimum is not None and value < minimum:
            invalid_count += 1
        elif maximum is not None and value > maximum:
            invalid_count += 1

    expected_parts: list[str] = []
    if minimum is not None:
        expected_parts.append(f"min>={minimum}")
    if maximum is not None:
        expected_parts.append(f"max<={maximum}")

    status = "PASS" if invalid_count == 0 else "FAIL"
    return build_result(
        check_id=f"{contract_id}.{column}.range",
        column_name=column,
        check_type="range",
        status=status,
        actual_value=f"min={min(numeric_values):.4f}, max={max(numeric_values):.4f}, mean={mean(numeric_values):.4f}",
        expected=", ".join(expected_parts),
        severity=severity_for_status(status),
        records_failing=invalid_count,
        message=(
            "Numeric values are within the allowed range."
            if status == "PASS"
            else "Observed numeric values violate the configured minimum/maximum bounds."
        ),
    )


def check_statistical_drift(
    records: list[dict[str, Any]],
    column: str,
    baselines: dict[str, Any],
    contract_id: str,
) -> dict[str, Any] | None:
    baseline_columns = baselines.get("columns", {})
    if column not in baseline_columns:
        return None

    values = collect_non_null_values(records, column)
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
        message = f"{column} mean drifted {z_score:.1f} stddev from baseline."
    elif z_score > 2:
        status = "WARN"
        severity = "WARNING"
        message = f"{column} mean within warning range ({z_score:.1f} stddev)."
    else:
        status = "PASS"
        severity = "LOW"
        message = f"{column} mean within baseline tolerance ({z_score:.1f} stddev)."

    return build_result(
        check_id=f"{contract_id}.{column}.statistical_drift",
        column_name=column,
        check_type="statistical_drift",
        status=status,
        actual_value=f"current_mean={current_mean:.4f}, z_score={z_score:.2f}",
        expected=f"baseline_mean={baseline_mean:.4f}, threshold<=2 WARN, <=3 FAIL",
        severity=severity,
        message=message,
    )


def update_baseline_candidates(records: list[dict[str, Any]], contract_schema: dict[str, Any]) -> dict[str, Any]:
    baseline_columns: dict[str, Any] = {}
    for column in contract_schema:
        values = collect_non_null_values(records, column)
        if not values:
            continue
        numeric_values = collect_numeric_values(values)
        if not numeric_values:
            continue
        if (len(numeric_values) / len(values)) < 0.95:
            continue
        baseline_columns[column] = {
            "mean": mean(numeric_values),
            "stddev": stddev(numeric_values),
        }
    return baseline_columns


def validate_timestamp_order(records: list[dict[str, Any]], earlier_col: str, later_col: str) -> tuple[int, list[str]]:
    failing: list[str] = []
    for idx, record in enumerate(records):
        earlier = extract_first(record, earlier_col)
        later = extract_first(record, later_col)
        earlier_dt = try_parse_datetime(earlier)
        later_dt = try_parse_datetime(later)
        if earlier_dt is None or later_dt is None:
            continue
        if later_dt < earlier_dt:
            failing.append(record_identifier(record, idx))
    return len(failing), failing[:5]


def validate_monotonic_sequence(records: list[dict[str, Any]], group_col: str, seq_col: str) -> tuple[int, list[str]]:
    groups: dict[str, list[tuple[int, int, str]]] = defaultdict(list)

    for idx, record in enumerate(records):
        group = extract_first(record, group_col)
        seq = extract_first(record, seq_col)
        ts = extract_first(record, "occurred_at")

        parsed_seq = try_parse_float(seq)
        if group is None or parsed_seq is None or not float(parsed_seq).is_integer():
            continue

        groups[safe_string(group)].append((idx, int(parsed_seq), safe_string(ts) if ts is not None else ""))

    failures: list[str] = []
    for group, items in groups.items():
        items.sort(key=lambda x: (x[2], x[1]))
        previous_seq: int | None = None
        for idx, seq, _ in items:
            if previous_seq is not None and seq <= previous_seq:
                failures.append(f"{group}:{record_identifier(records[idx], idx)}")
            previous_seq = seq

    return len(failures), failures[:5]


def parse_quality_checks(contract: dict[str, Any]) -> list[str]:
    quality = contract.get("quality", {})
    specification = quality.get("specification", {})
    checks: list[str] = []
    for value in specification.values():
        if isinstance(value, list):
            checks.extend([str(v) for v in value])
    return checks


def run_quality_checks(records: list[dict[str, Any]], contract: dict[str, Any]) -> list[dict[str, Any]]:
    contract_id = contract.get("id", "unknown_contract")
    checks = parse_quality_checks(contract)
    results: list[dict[str, Any]] = []

    for check in checks:
        stripped = check.strip()

        if stripped == "recorded_at >= occurred_at":
            failing_count, sample = validate_timestamp_order(records, "occurred_at", "recorded_at")
            status = "PASS" if failing_count == 0 else "FAIL"
            results.append(
                build_result(
                    check_id=f"{contract_id}.quality.recorded_at_gte_occurred_at",
                    column_name="recorded_at,occurred_at",
                    check_type="quality_rule",
                    status=status,
                    actual_value=f"violations={failing_count}",
                    expected="recorded_at >= occurred_at",
                    severity=severity_for_status(status),
                    records_failing=failing_count,
                    sample_failing=sample,
                    message=(
                        "All records satisfy recorded_at >= occurred_at."
                        if status == "PASS"
                        else "Some records violate recorded_at >= occurred_at."
                    ),
                )
            )
        elif stripped == "sequence_number is_monotonic_per aggregate_id":
            failing_count, sample = validate_monotonic_sequence(records, "aggregate_id", "sequence_number")
            status = "PASS" if failing_count == 0 else "FAIL"
            results.append(
                build_result(
                    check_id=f"{contract_id}.quality.sequence_monotonic_per_aggregate",
                    column_name="aggregate_id,sequence_number",
                    check_type="quality_rule",
                    status=status,
                    actual_value=f"violations={failing_count}",
                    expected="sequence_number is_monotonic_per aggregate_id",
                    severity=severity_for_status(status),
                    records_failing=failing_count,
                    sample_failing=sample,
                    message=(
                        "All aggregate streams are monotonic by sequence_number."
                        if status == "PASS"
                        else "Some aggregate streams violate monotonic sequence ordering."
                    ),
                )
            )
        elif stripped == "row_count >= 1":
            status = "PASS" if len(records) >= 1 else "FAIL"
            results.append(
                build_result(
                    check_id=f"{contract_id}.quality.row_count",
                    column_name="__row_count__",
                    check_type="quality_rule",
                    status=status,
                    actual_value=f"row_count={len(records)}",
                    expected="row_count >= 1",
                    severity=severity_for_status(status),
                    records_failing=0 if status == "PASS" else 1,
                    message="Row count check.",
                )
            )

    return results


def validate_contract(records: list[dict[str, Any]], contract: dict[str, Any], data_path: str | Path, mode: str = "AUDIT") -> dict[str, Any]:
    contract_id = contract.get("id", "unknown_contract")
    contract_schema = contract.get("schema", {})
    results: list[dict[str, Any]] = []

    baselines_path = Path("schema_snapshots") / f"{contract_id}_baselines.json"
    baselines = load_baselines(baselines_path)

    for column, clause in contract_schema.items():
        required_result = check_required(records, column, bool(clause.get("required")), contract_id)
        if required_result is not None:
            results.append(required_result)

        expected_type = clause.get("type")
        if expected_type:
            results.append(check_type(records, column, expected_type, contract_id))

        if "enum" in clause:
            results.append(check_enum(records, column, clause["enum"], contract_id))

        if clause.get("format") == "uuid":
            results.append(check_uuid_format(records, column, contract_id))
        elif clause.get("format") == "date-time":
            results.append(check_datetime_format(records, column, contract_id))

        if "minimum" in clause or "maximum" in clause:
            results.append(
                check_range(
                    records,
                    column,
                    clause.get("minimum"),
                    clause.get("maximum"),
                    contract_id,
                )
            )

        drift_result = check_statistical_drift(records, column, baselines, contract_id)
        if drift_result is not None:
            results.append(drift_result)

    results.extend(run_quality_checks(records, contract))

    baseline_candidates = update_baseline_candidates(records, contract_schema)
    if baseline_candidates:
        write_baselines(baselines_path, baseline_candidates)

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    warned = sum(1 for r in results if r["status"] == "WARN")
    errored = sum(1 for r in results if r["status"] == "ERROR")

    blocking_count = compute_blocking_violation_count(results, mode)

    return {
        "report_id": str(uuid.uuid4()),
        "contract_id": contract_id,
        "snapshot_id": sha256_of_file(data_path),
        "run_timestamp": to_iso_now(),
        "enforcement_mode": mode,
        "blocking_violation_count": blocking_count,
        "would_block_pipeline": blocking_count > 0,
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
    report = validate_contract(records, contract, args.data, mode=args.mode)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"[OK] contract={args.contract}")
    print(f"[OK] data={args.data}")
    print(f"[OK] output={output_path}")
    print(f"[OK] mode={report['enforcement_mode']} would_block={report['would_block_pipeline']} blocking_violations={report['blocking_violation_count']}")
    print(
        "[OK] summary="
        f"total={report['total_checks']} passed={report['passed']} failed={report['failed']} warned={report['warned']} errored={report['errored']}"
    )

    if args.fail_on_block and report["would_block_pipeline"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""SchemaEvolutionAnalyzer for TRP1 Week 8: Data Contract Enforcer.

Diffs timestamped schema snapshots for a contract, classifies each change,
and writes a compatibility verdict plus migration impact report.

Enhancements over the earlier version:
- explicit CRITICAL severity for narrow type/range changes
- richer per-consumer failure mode analysis using registry + lineage context
- subscriber-specific migration checklist generation
- explicit handling of confidence scale narrowing (0.0-1.0 -> 0-100)
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


BREAKING = "BREAKING"
COMPATIBLE = "COMPATIBLE"
NO_CHANGE = "NO_CHANGE"

CRITICAL = "CRITICAL"
HIGH = "HIGH"
MEDIUM = "MEDIUM"
INFO = "INFO"

SEVERITY_ORDER = {
    CRITICAL: 4,
    HIGH: 3,
    MEDIUM: 2,
    INFO: 1,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze schema evolution across saved contract snapshots")
    parser.add_argument("--contract-id", required=True, help="Contract identifier matching schema_snapshots/<contract-id>/")
    parser.add_argument(
        "--since",
        default="7 days ago",
        help="Relative lookback window such as '7 days ago' (default: 7 days ago)",
    )
    parser.add_argument("--output", required=True, help="Path to primary analyzer output JSON")
    parser.add_argument(
        "--registry",
        default="contract_registry/subscriptions.yaml",
        help="Optional registry YAML for subscriber-aware migration analysis",
    )
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_since_expression(expr: str) -> datetime:
    text = expr.strip().lower()
    now = datetime.now(timezone.utc)

    m = re.fullmatch(r"(\d+)\s+days?\s+ago", text)
    if m:
        return now - timedelta(days=int(m.group(1)))

    m = re.fullmatch(r"(\d+)\s+hours?\s+ago", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    if text == "yesterday":
        return now - timedelta(days=1)
    if text == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    return now - timedelta(days=7)


def parse_snapshot_timestamp(path: Path) -> datetime:
    return datetime.strptime(path.stem, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def list_snapshots(contract_id: str, since_expr: str) -> list[Path]:
    snapshot_dir = Path("schema_snapshots") / contract_id
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"No snapshot directory found for contract: {snapshot_dir}")

    since_dt = parse_since_expression(since_expr)
    snapshots = sorted(snapshot_dir.glob("*.yaml"))
    filtered = [p for p in snapshots if parse_snapshot_timestamp(p) >= since_dt]

    if len(filtered) >= 2:
        return filtered
    return snapshots


def load_registry(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}
    return payload if isinstance(payload, dict) else {}


def contract_downstream(contract_yaml: dict[str, Any]) -> list[dict[str, Any]]:
    downstream = contract_yaml.get("lineage", {}).get("downstream", []) or []
    out: list[dict[str, Any]] = []
    for d in downstream:
        if isinstance(d, dict) and d.get("id"):
            out.append(d)
    return out


def registry_subscribers(contract_id: str, registry: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sub in registry.get("subscriptions", []) or []:
        if not isinstance(sub, dict):
            continue
        if str(sub.get("contract_id", "")) == contract_id:
            out.append(sub)
    return out


def merged_consumers(contract_id: str, contract_yaml: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    for sub in registry_subscribers(contract_id, registry):
        sub_id = str(sub.get("subscriber_id", ""))
        if sub_id and sub_id not in seen:
            seen.add(sub_id)
            merged.append(
                {
                    "id": sub_id,
                    "subscriber_team": sub.get("subscriber_team"),
                    "fields_consumed": sub.get("fields_consumed", []),
                    "breaking_fields": sub.get("breaking_fields", []),
                    "validation_mode": sub.get("validation_mode"),
                    "contact": sub.get("contact"),
                    "source": "registry",
                }
            )

    for d in contract_downstream(contract_yaml):
        sub_id = str(d.get("id", ""))
        if sub_id and sub_id not in seen:
            seen.add(sub_id)
            merged.append(
                {
                    "id": sub_id,
                    "subscriber_team": d.get("subscriber_team"),
                    "fields_consumed": d.get("fields_consumed", []),
                    "breaking_fields": d.get("breaking_if_changed", []),
                    "validation_mode": d.get("validation_mode"),
                    "contact": d.get("contact"),
                    "source": "lineage",
                }
            )

    return merged


def field_consumed_by_subscriber(field_name: str, subscriber: dict[str, Any]) -> bool:
    fields = subscriber.get("fields_consumed", []) or []
    breaking_fields = subscriber.get("breaking_fields", []) or []

    flattened: list[str] = []
    for item in fields + breaking_fields:
        if isinstance(item, str):
            flattened.append(item)
        elif isinstance(item, dict) and item.get("field"):
            flattened.append(str(item.get("field")))

    if field_name in flattened:
        return True

    normalized = field_name.replace("[*].", ".").replace("[*]", "")
    return normalized in flattened


def is_confidence_scale_break(old_clause: dict[str, Any], new_clause: dict[str, Any]) -> bool:
    return (
        old_clause.get("type") == "number"
        and new_clause.get("type") in {"integer", "number"}
        and old_clause.get("minimum") == 0.0
        and old_clause.get("maximum") == 1.0
        and new_clause.get("minimum") == 0
        and new_clause.get("maximum") == 100
    )


def is_narrowing_range(old_clause: dict[str, Any], new_clause: dict[str, Any]) -> bool:
    old_min = old_clause.get("minimum")
    new_min = new_clause.get("minimum")
    old_max = old_clause.get("maximum")
    new_max = new_clause.get("maximum")

    narrowed_min = old_min is not None and new_min is not None and new_min > old_min
    narrowed_max = old_max is not None and new_max is not None and new_max < old_max
    return narrowed_min or narrowed_max


def classify_change(
    field_name: str,
    old_clause: dict[str, Any] | None,
    new_clause: dict[str, Any] | None,
) -> tuple[str, str, str]:
    if old_clause is None and new_clause is not None:
        if new_clause.get("required", False):
            return BREAKING, HIGH, "Added required field; existing producers/consumers may break."
        return COMPATIBLE, INFO, "Added nullable field; consumers can ignore it."

    if old_clause is not None and new_clause is None:
        return BREAKING, CRITICAL, "Removed field; downstream parsers and readers may fail."

    if old_clause is None and new_clause is None:
        return NO_CHANGE, INFO, "No material change."

    # From here onward, both are guaranteed non-None.
    assert old_clause is not None
    assert new_clause is not None

    old_type = old_clause.get("type")
    new_type = new_clause.get("type")

    if is_confidence_scale_break(old_clause, new_clause):
        return BREAKING, CRITICAL, "Confidence scale narrowed from 0.0-1.0 to 0-100; threshold logic becomes invalid."

    if old_type != new_type:
        return BREAKING, CRITICAL, f"Type change {old_type} -> {new_type}. Explicit migration and rollback required."

    old_required = bool(old_clause.get("required", False))
    new_required = bool(new_clause.get("required", False))
    if old_required != new_required:
        if new_required and not old_required:
            return BREAKING, HIGH, "Field became required; existing producers may emit null/missing values."
        return COMPATIBLE, INFO, "Field became optional; backward-compatible change."

    old_min = old_clause.get("minimum")
    new_min = new_clause.get("minimum")
    old_max = old_clause.get("maximum")
    new_max = new_clause.get("maximum")
    if old_min != new_min or old_max != new_max:
        if is_narrowing_range(old_clause, new_clause):
            return BREAKING, CRITICAL, f"Allowed value range narrowed: min {old_min} -> {new_min}, max {old_max} -> {new_max}."
        return COMPATIBLE, MEDIUM, f"Allowed value range changed: min {old_min} -> {new_min}, max {old_max} -> {new_max}."

    old_pattern = old_clause.get("pattern")
    new_pattern = new_clause.get("pattern")
    if old_pattern != new_pattern:
        return BREAKING, HIGH, f"Pattern constraint changed: {old_pattern} -> {new_pattern}."

    old_format = old_clause.get("format")
    new_format = new_clause.get("format")
    if old_format != new_format:
        return BREAKING, HIGH, f"Format constraint changed: {old_format} -> {new_format}."

    old_enum = old_clause.get("enum")
    new_enum = new_clause.get("enum")
    if old_enum != new_enum:
        old_set = set(old_enum or [])
        new_set = set(new_enum or [])
        removed = sorted(old_set - new_set)
        added = sorted(new_set - old_set)
        if removed:
            return BREAKING, HIGH, f"Enum values removed: {removed}."
        if added:
            return COMPATIBLE, INFO, f"Enum values added: {added}."

    old_desc = old_clause.get("description")
    new_desc = new_clause.get("description")
    if old_desc != new_desc:
        return COMPATIBLE, INFO, "Documentation changed without altering executable contract."

    return NO_CHANGE, INFO, "No material change."


def render_diff(old_clause: dict[str, Any] | None, new_clause: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "before": old_clause,
        "after": new_clause,
    }


def summarize_failure_modes(field_name: str, verdict: str, severity: str, rationale: str, consumers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    impacts: list[dict[str, Any]] = []

    for consumer in consumers:
        if not field_consumed_by_subscriber(field_name, consumer):
            continue

        consumer_id = str(consumer.get("id", "unknown-consumer"))
        mode = consumer.get("validation_mode")
        team = consumer.get("subscriber_team")
        contact = consumer.get("contact")

        if verdict == BREAKING:
            failure_mode = (
                f"{consumer_id} may reject payloads, fail parsing, or silently misinterpret '{field_name}'. "
                f"{rationale}"
            )
        elif verdict == COMPATIBLE:
            failure_mode = (
                f"{consumer_id} should continue functioning, but may need optional adoption of '{field_name}'. "
                f"{rationale}"
            )
        else:
            failure_mode = f"No expected subscriber impact for {consumer_id} on '{field_name}'."

        impacts.append(
            {
                "subscriber_id": consumer_id,
                "subscriber_team": team,
                "validation_mode": mode,
                "contact": contact,
                "severity": severity,
                "failure_mode": failure_mode,
                "source": consumer.get("source"),
            }
        )

    if not impacts:
        impacts.append(
            {
                "subscriber_id": "generic-downstream",
                "subscriber_team": None,
                "validation_mode": None,
                "contact": None,
                "severity": severity,
                "failure_mode": (
                    f"Generic downstream impact: consumers reading '{field_name}' may misinterpret or reject changed values. "
                    f"{rationale}"
                ),
                "source": "fallback",
            }
        )

    return impacts


def ordered_migration_checklist(contract_id: str, changes: list[dict[str, Any]], consumers: list[dict[str, Any]]) -> list[str]:
    checklist = [
        f"Review the previous and latest snapshots for contract {contract_id} and confirm each changed field before rollout.",
    ]

    for consumer in consumers:
        consumer_id = consumer.get("id")
        contact = consumer.get("contact")
        mode = consumer.get("validation_mode")
        if consumer_id:
            line = f"Notify subscriber {consumer_id}"
            if contact:
                line += f" ({contact})"
            if mode:
                line += f" and confirm validation mode {mode}"
            line += " before deployment."
            checklist.append(line)

    for change in changes:
        field_name = change["field_name"]
        verdict = change["verdict"]
        severity = change["severity"]

        if verdict == BREAKING:
            checklist.append(
                f"Patch all producers and subscribers touching '{field_name}' before rollout; severity={severity}."
            )
            if "confidence" in field_name.lower():
                checklist.append(
                    f"Update threshold logic for '{field_name}' so consumers do not treat 0-100 scores as 0.0-1.0 floats."
                )
        elif verdict == COMPATIBLE:
            checklist.append(
                f"Optionally update subscribers to consume '{field_name}' if needed; no hard migration required."
            )

    checklist.extend(
        [
            "Run ValidationRunner on a clean baseline after migration.",
            "Regenerate contracts and schema snapshots after migration completes.",
        ]
    )
    return checklist


def rollback_plan(contract_id: str, previous_snapshot: Path) -> list[str]:
    return [
        f"Restore the previous snapshot-backed contract for {contract_id} using {previous_snapshot.name} as the rollback target.",
        "Revert the producer-side code change that introduced the incompatible schema diff.",
        "Re-run validation against the restored dataset and confirm critical failures clear.",
    ]


def compute_compatibility_verdict(changes: list[dict[str, Any]]) -> str:
    if any(change["verdict"] == BREAKING for change in changes):
        return BREAKING
    if any(change["verdict"] == COMPATIBLE for change in changes):
        return COMPATIBLE
    return NO_CHANGE


def analyze_pair(
    contract_id: str,
    old_path: Path,
    new_path: Path,
    registry: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    old_yaml = load_yaml(old_path)
    new_yaml = load_yaml(new_path)

    old_schema = old_yaml.get("schema", {})
    new_schema = new_yaml.get("schema", {})
    consumers = merged_consumers(contract_id, new_yaml, registry)

    all_fields = sorted(set(old_schema.keys()) | set(new_schema.keys()))
    changes: list[dict[str, Any]] = []

    for field in all_fields:
        old_clause = old_schema.get(field)
        new_clause = new_schema.get(field)
        verdict, severity, rationale = classify_change(field, old_clause, new_clause)
        if verdict == NO_CHANGE:
            continue

        consumer_impacts = summarize_failure_modes(field, verdict, severity, rationale, consumers)

        changes.append(
            {
                "field_name": field,
                "verdict": verdict,
                "severity": severity,
                "rationale": rationale,
                "diff": render_diff(old_clause, new_clause),
                "consumer_failure_modes": consumer_impacts,
            }
        )

    return changes, old_yaml, new_yaml, consumers


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def main() -> None:
    args = parse_args()
    snapshots = list_snapshots(args.contract_id, args.since)
    if len(snapshots) < 2:
        raise ValueError(
            f"Need at least 2 schema snapshots to analyze evolution for {args.contract_id}; found {len(snapshots)}"
        )

    registry = load_registry(args.registry)

    old_path = snapshots[-2]
    new_path = snapshots[-1]
    changes, old_yaml, new_yaml, consumers = analyze_pair(args.contract_id, old_path, new_path, registry)

    compatibility_verdict = compute_compatibility_verdict(changes)
    breaking_fields = [c["field_name"] for c in changes if c["verdict"] == BREAKING]

    output = {
        "contract_id": args.contract_id,
        "generated_at": iso_now(),
        "since": args.since,
        "snapshot_before": str(old_path),
        "snapshot_after": str(new_path),
        "compatibility_verdict": compatibility_verdict,
        "change_count": len(changes),
        "critical_change_count": sum(1 for c in changes if c["severity"] == CRITICAL),
        "changes": changes,
        "full_blast_radius": {
            "affected_nodes": [c.get("id") for c in consumers if c.get("id")],
            "affected_pipelines": [c.get("id") for c in consumers if "pipeline" in str(c.get("id", "")).lower()],
            "affected_subscribers": consumers,
        },
        "migration_checklist": ordered_migration_checklist(args.contract_id, changes, consumers),
        "rollback_plan": rollback_plan(args.contract_id, old_path),
        "breaking_fields": breaking_fields,
    }

    output_path = Path(args.output)
    write_json(output_path, output)

    migration_report_path = output_path.parent / (
        f"migration_impact_{args.contract_id}_{parse_snapshot_timestamp(new_path).strftime('%Y%m%d_%H%M%S')}.json"
    )
    write_json(migration_report_path, output)

    print(f"[OK] contract_id={args.contract_id}")
    print(f"[OK] snapshot_before={old_path}")
    print(f"[OK] snapshot_after={new_path}")
    print(f"[OK] change_count={len(changes)}")
    print(f"[OK] critical_change_count={output['critical_change_count']}")
    print(f"[OK] compatibility_verdict={compatibility_verdict}")
    print(f"[OK] output={output_path}")
    print(f"[OK] migration_report={migration_report_path}")


if __name__ == "__main__":
    main()

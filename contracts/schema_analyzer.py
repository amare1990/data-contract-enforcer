#!/usr/bin/env python3
"""SchemaEvolutionAnalyzer for TRP1 Week 8: Data Contract Enforcer.

Diffs timestamped schema snapshots for a contract, classifies each change,
and writes a compatibility verdict plus migration impact report.

Example:
    uv run python contracts/schema_analyzer.py \
      --contract-id week3-document-refinery-extractions \
      --output validation_reports/schema_evolution_week3.json
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


SEVERITY_ORDER = {
    BREAKING: 3,
    COMPATIBLE: 2,
    NO_CHANGE: 1,
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
    return parser.parse_args()


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

    # fallback: treat unknown values as 7 days ago
    return now - timedelta(days=7)


def parse_snapshot_timestamp(path: Path) -> datetime:
    return datetime.strptime(path.stem, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_snapshots(contract_id: str, since_expr: str) -> list[Path]:
    snapshot_dir = Path("schema_snapshots") / contract_id
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"No snapshot directory found for contract: {snapshot_dir}")

    since_dt = parse_since_expression(since_expr)
    snapshots = sorted(snapshot_dir.glob("*.yaml"))
    filtered = [p for p in snapshots if parse_snapshot_timestamp(p) >= since_dt]

    # If the since-window returns too few snapshots, fall back to all snapshots.
    if len(filtered) >= 2:
        return filtered
    return snapshots


def classify_change(field_name: str, old_clause: dict[str, Any] | None, new_clause: dict[str, Any] | None) -> tuple[str, str]:
    if old_clause is None and new_clause is not None:
        if new_clause.get("required", False):
            return BREAKING, "Add non-nullable column — coordinate with all producers before rollout."
        return COMPATIBLE, "Add nullable column — downstream consumers can ignore it."

    if old_clause is not None and new_clause is None:
        return BREAKING, "Remove column — deprecation period mandatory and blast radius review required."

    if old_clause is None and new_clause is None:
        return NO_CHANGE, "No material change."

    old_type = old_clause.get("type")
    new_type = new_clause.get("type")
    if old_type != new_type:
        return BREAKING, f"Type change {old_type} -> {new_type}. Explicit migration and rollback plan required."

    old_required = bool(old_clause.get("required", False))
    new_required = bool(new_clause.get("required", False))
    if old_required != new_required:
        if new_required and not old_required:
            return BREAKING, "Field became required — existing producers may fail or emit nulls."
        return COMPATIBLE, "Field became optional — backward-compatible for existing producers and consumers."

    old_min = old_clause.get("minimum")
    new_min = new_clause.get("minimum")
    old_max = old_clause.get("maximum")
    new_max = new_clause.get("maximum")
    if old_min != new_min or old_max != new_max:
        return BREAKING, f"Range change detected: minimum {old_min} -> {new_min}, maximum {old_max} -> {new_max}."

    old_pattern = old_clause.get("pattern")
    new_pattern = new_clause.get("pattern")
    if old_pattern != new_pattern:
        return BREAKING, f"Pattern constraint changed: {old_pattern} -> {new_pattern}."

    old_format = old_clause.get("format")
    new_format = new_clause.get("format")
    if old_format != new_format:
        return BREAKING, f"Format constraint changed: {old_format} -> {new_format}."

    old_enum = old_clause.get("enum")
    new_enum = new_clause.get("enum")
    if old_enum != new_enum:
        old_set = set(old_enum or [])
        new_set = set(new_enum or [])
        removed = sorted(old_set - new_set)
        added = sorted(new_set - old_set)
        if removed:
            return BREAKING, f"Enum values removed: {removed}."
        if added:
            return COMPATIBLE, f"Enum values added: {added}."

    old_desc = old_clause.get("description")
    new_desc = new_clause.get("description")
    if old_desc != new_desc:
        return COMPATIBLE, "Documentation/description changed without altering the executable contract."

    return NO_CHANGE, "No material change."


def render_diff(old_clause: dict[str, Any] | None, new_clause: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "before": old_clause,
        "after": new_clause,
    }


def summarize_failure_mode(field_name: str, verdict: str, rationale: str) -> str:
    if verdict == BREAKING:
        return f"Downstream consumers reading '{field_name}' may fail validation or silently misinterpret values. {rationale}"
    if verdict == COMPATIBLE:
        return f"Consumers should remain functional, but teams should review '{field_name}' for optional adoption. {rationale}"
    return f"No expected downstream failure for '{field_name}'. {rationale}"


def ordered_migration_checklist(contract_id: str, breaking_fields: list[str]) -> list[str]:
    checklist = [
        f"Review the latest and previous snapshots for contract {contract_id} and confirm each changed field.",
        "Notify downstream consumers before applying the new schema in production.",
    ]
    for field in breaking_fields:
        checklist.append(f"Patch producers and consumers that read or write '{field}'.")
    checklist.extend(
        [
            "Run ValidationRunner against a clean baseline after migration.",
            "Regenerate contracts and schema snapshots after the migration is complete.",
        ]
    )
    return checklist


def rollback_plan(contract_id: str, previous_snapshot: Path) -> list[str]:
    return [
        f"Restore the previous snapshot-backed contract for {contract_id} using {previous_snapshot.name} as the rollback target.",
        "Revert the producer-side code change that introduced the incompatible schema diff.",
        "Re-run validation against the restored dataset and confirm critical failures clear.",
    ]


def contract_downstream(contract_yaml: dict[str, Any]) -> list[str]:
    downstream = contract_yaml.get("lineage", {}).get("downstream", []) or []
    return [d.get("id") for d in downstream if isinstance(d, dict) and d.get("id")]


def compute_compatibility_verdict(changes: list[dict[str, Any]]) -> str:
    if any(change["verdict"] == BREAKING for change in changes):
        return BREAKING
    if any(change["verdict"] == COMPATIBLE for change in changes):
        return COMPATIBLE
    return NO_CHANGE


def analyze_pair(old_path: Path, new_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    old_yaml = load_yaml(old_path)
    new_yaml = load_yaml(new_path)

    old_schema = old_yaml.get("schema", {})
    new_schema = new_yaml.get("schema", {})

    all_fields = sorted(set(old_schema.keys()) | set(new_schema.keys()))
    changes: list[dict[str, Any]] = []

    for field in all_fields:
        old_clause = old_schema.get(field)
        new_clause = new_schema.get(field)
        verdict, rationale = classify_change(field, old_clause, new_clause)
        if verdict == NO_CHANGE:
            continue
        changes.append(
            {
                "field_name": field,
                "verdict": verdict,
                "rationale": rationale,
                "diff": render_diff(old_clause, new_clause),
                "consumer_failure_mode": summarize_failure_mode(field, verdict, rationale),
            }
        )

    return changes, old_yaml, new_yaml


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

    old_path = snapshots[-2]
    new_path = snapshots[-1]
    changes, old_yaml, new_yaml = analyze_pair(old_path, new_path)

    compatibility_verdict = compute_compatibility_verdict(changes)
    downstream_nodes = contract_downstream(new_yaml)
    breaking_fields = [c["field_name"] for c in changes if c["verdict"] == BREAKING]

    output = {
        "contract_id": args.contract_id,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "since": args.since,
        "snapshot_before": str(old_path),
        "snapshot_after": str(new_path),
        "compatibility_verdict": compatibility_verdict,
        "change_count": len(changes),
        "changes": changes,
        "full_blast_radius": {
            "affected_nodes": downstream_nodes,
            "affected_pipelines": [n for n in downstream_nodes if "pipeline" in str(n).lower()],
        },
        "migration_checklist": ordered_migration_checklist(args.contract_id, breaking_fields),
        "rollback_plan": rollback_plan(args.contract_id, old_path),
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
    print(f"[OK] compatibility_verdict={compatibility_verdict}")
    print(f"[OK] output={output_path}")
    print(f"[OK] migration_report={migration_report_path}")


if __name__ == "__main__":
    main()

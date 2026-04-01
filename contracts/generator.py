#!/usr/bin/env python3
"""ContractGenerator for TRP1 Week 8: Data Contract Enforcer.

Generates Bitol-compatible YAML contracts, dbt schema YAML, and timestamped
schema snapshots from JSONL outputs.

Primary design goals:
- deterministic and evaluator-friendly CLI
- readable contract output
- strong support for Week 3 extractions and Week 5 events
- graceful degradation on imperfect input

Example:
    python contracts/generator.py \
      --source outputs/week3/extractions.jsonl \
      --contract-id week3-document-refinery-extractions \
      --lineage outputs/week4/lineage_snapshots.jsonl \
      --output generated_contracts/
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import yaml


UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
GIT_SHA40_RE = re.compile(r"^[a-f0-9]{40}$")
PASCAL_CASE_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
ISO_8601_Z_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)


@dataclass
class ColumnProfile:
    name: str
    path: str
    dtype: str
    null_fraction: float
    cardinality_estimate: int
    sample_values: list[str]
    stats: dict[str, float] | None = None
    dominant_pattern: str | None = None


@dataclass
class ContractContext:
    contract_id: str
    title: str
    owner: str
    description: str
    logical_name: str
    source_path: str
    top_level_schema_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate data contracts from JSONL outputs")
    parser.add_argument("--source", required=True, help="Path to JSONL source file")
    parser.add_argument("--contract-id", required=True, help="Stable contract identifier")
    parser.add_argument("--lineage", required=False, help="Path to lineage snapshot JSONL")
    parser.add_argument("--output", required=True, help="Directory for generated contracts")
    parser.add_argument(
        "--owner",
        default="week8-team",
        help="Contract owner label written into YAML",
    )
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


def load_latest_lineage_snapshot(lineage_path: str | Path | None) -> dict[str, Any] | None:
    if not lineage_path:
        return None

    records = load_jsonl(lineage_path)
    latest = records[-1]
    return latest


def infer_context(contract_id: str, source_path: str, owner: str) -> ContractContext:
    sp = source_path.replace("\\", "/")

    if "week3" in sp and "extractions" in sp:
        return ContractContext(
            contract_id=contract_id,
            title="Week 3 Document Refinery - Extraction Records",
            owner=owner,
            description=(
                "One record per processed document. Each record contains extracted facts, "
                "entity references, processing metadata, and lineage-relevant identifiers."
            ),
            logical_name="extractions",
            source_path=sp,
            top_level_schema_name="extraction_record",
        )
    if "week5" in sp and "events" in sp:
        return ContractContext(
            contract_id=contract_id,
            title="Week 5 Event Sourcing Platform - Event Records",
            owner=owner,
            description=(
                "One record per domain event in the event store, including payload, "
                "event metadata, schema version, and event timing."
            ),
            logical_name="events",
            source_path=sp,
            top_level_schema_name="event_record",
        )
    if "week4" in sp and "lineage" in sp:
        return ContractContext(
            contract_id=contract_id,
            title="Week 4 Brownfield Cartographer - Lineage Snapshots",
            owner=owner,
            description="Lineage graph snapshots with nodes, edges, and capture metadata.",
            logical_name="lineage_snapshots",
            source_path=sp,
            top_level_schema_name="lineage_snapshot",
        )
    if "traces" in sp or "runs.jsonl" in sp:
        return ContractContext(
            contract_id=contract_id,
            title="LangSmith Trace Records",
            owner=owner,
            description="Trace export records used for AI contract enforcement and observability.",
            logical_name="traces",
            source_path=sp,
            top_level_schema_name="trace_record",
        )

    title = contract_id.replace("-", " ").replace("_", " ").title()
    logical_name = Path(source_path).stem
    return ContractContext(
        contract_id=contract_id,
        title=title,
        owner=owner,
        description="Auto-generated data contract from observed JSONL output.",
        logical_name=logical_name,
        source_path=sp,
        top_level_schema_name=logical_name,
    )


def flatten_records(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Flatten nested JSON into a profile-friendly DataFrame.

    Strategy:
    - Keep scalar top-level fields as columns.
    - For list[dict] fields, explode to one row per nested item.
    - Prefix nested item keys with the singularized list name where practical.
    - Preserve original top-level identifiers to retain semantic traceability.
    """
    rows: list[dict[str, Any]] = []

    for record in records:
        base: dict[str, Any] = {}
        nested_dicts: dict[str, dict[str, Any]] = {}
        nested_lists_of_dicts: dict[str, list[dict[str, Any]]] = {}
        nested_lists_of_scalars: dict[str, list[Any]] = {}

        for key, value in record.items():
            if isinstance(value, dict):
                nested_dicts[key] = value
            elif isinstance(value, list):
                if value and all(isinstance(item, dict) for item in value):
                    nested_lists_of_dicts[key] = value
                else:
                    nested_lists_of_scalars[key] = value
            else:
                base[key] = value

        for key, value in nested_dicts.items():
            for child_key, child_value in value.items():
                base[f"{key}.{child_key}"] = child_value

        for key, value in nested_lists_of_scalars.items():
            base[key] = json.dumps(value, ensure_ascii=False)
            base[f"{key}.__len__"] = len(value)

        if not nested_lists_of_dicts:
            rows.append(base)
            continue

        list_rows: list[dict[str, Any]] = []
        for list_key, items in nested_lists_of_dicts.items():
            prefix = singularize(list_key)
            if not items:
                list_rows.append(dict(base))
                continue
            for item in items:
                row = dict(base)
                for child_key, child_value in item.items():
                    row[f"{prefix}.{child_key}"] = child_value
                row[f"{list_key}.__len__"] = len(items)
                list_rows.append(row)

        rows.extend(list_rows)

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("Flattened DataFrame is empty; source data may be malformed")
    return df


def singularize(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s") and len(name) > 1:
        return name[:-1]
    return name


def dominant_character_pattern(series: pd.Series) -> str | None:
    values = [str(v) for v in series.dropna().astype(str).head(200)]
    if not values:
        return None

    def classify(s: str) -> str:
        chunks: list[str] = []
        for ch in s:
            if ch.isdigit():
                chunks.append("9")
            elif ch.isalpha():
                chunks.append("A")
            elif ch.isspace():
                chunks.append(" ")
            else:
                chunks.append(ch)
        return "".join(chunks)

    counts = Counter(classify(v) for v in values)
    return counts.most_common(1)[0][0]


def numeric_stats(series: pd.Series) -> dict[str, float] | None:
    """Safely compute numeric stats without relying on pandas.to_numeric on object-heavy data."""
    values: list[float] = []

    non_null_values = series.dropna().tolist()

    for value in non_null_values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if math.isfinite(float(value)):
                values.append(float(value))
            continue
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            try:
                parsed = float(text)
            except ValueError:
                continue
            if math.isfinite(parsed):
                values.append(parsed)

    if not values:
        return None

    numeric_ratio = len(values) / max(len(non_null_values), 1)
    if numeric_ratio < 0.95:
        return None

    clean = pd.Series(values, dtype="float64")
    stddev = clean.std()
    stats = {
        "min": float(clean.min()),
        "max": float(clean.max()),
        "mean": float(clean.mean()),
        "p25": float(clean.quantile(0.25)),
        "p50": float(clean.quantile(0.50)),
        "p75": float(clean.quantile(0.75)),
        "p95": float(clean.quantile(0.95)),
        "p99": float(clean.quantile(0.99)),
        "stddev": float(0.0 if math.isnan(stddev) else stddev),
    }
    return stats


def build_column_profiles(df: pd.DataFrame) -> dict[str, ColumnProfile]:
    profiles: dict[str, ColumnProfile] = {}
    for col in df.columns:
        series = df[col]
        non_null = series.dropna().tolist()
        sample_values = [safe_string(v) for v in non_null[:5]]
        cardinality_estimate = len({safe_string(v) for v in non_null})
        profile = ColumnProfile(
            name=col,
            path=col,
            dtype=str(series.dtype),
            null_fraction=float(series.isna().mean()),
            cardinality_estimate=int(cardinality_estimate),
            sample_values=sample_values,
            stats=numeric_stats(series),
            dominant_pattern=dominant_character_pattern(series)
            if str(series.dtype) in {"object", "string"}
            else None,
        )
        profiles[col] = profile
    return profiles


def safe_string(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def infer_clause(profile: ColumnProfile) -> dict[str, Any]:
    clause: dict[str, Any] = {
        "type": infer_type(profile),
        "required": profile.null_fraction == 0.0,
    }

    desc_parts: list[str] = []

    if profile.name.endswith("_id") or profile.name.endswith(".id"):
        if looks_like_uuid_samples(profile.sample_values):
            clause["format"] = "uuid"
            desc_parts.append("Identifier field; UUID format verified from observed values.")
        else:
            desc_parts.append("Identifier field; observed values are not UUID-formatted.")

    if profile.name.endswith("_at") or profile.name.endswith("_time") or profile.name in {"start_time", "end_time"}:
        if looks_like_datetime(profile.sample_values):
            clause["format"] = "date-time"
            desc_parts.append("Timestamp field in ISO 8601 format.")

    if profile.stats is not None and is_confidence_field(profile.name):
        clause["type"] = "number"
        clause["minimum"] = 0.0
        clause["maximum"] = 1.0
        desc_parts.append(
            "Confidence score. Must remain 0.0-1.0 float. Breaking change if converted to 0-100 scale."
        )

    if profile.stats is not None and (profile.name.endswith("processing_time_ms") or profile.name.endswith(".processing_time_ms")):
        clause["type"] = "integer"
        clause["minimum"] = 1
        desc_parts.append("Positive processing latency in milliseconds.")

    if profile.stats is not None and profile.name.endswith("total_cost"):
        clause["type"] = "number"
        clause["minimum"] = 0.0
        desc_parts.append("Non-negative cost in USD.")

    if profile.stats is not None and profile.name.endswith("sequence_number"):
        clause["type"] = "integer"
        clause["minimum"] = 1
        desc_parts.append("Monotonic sequence number per aggregate.")

    if clause["type"] == "string" and profile.cardinality_estimate > 0 and profile.cardinality_estimate <= 10:
        if len(profile.sample_values) == profile.cardinality_estimate:
            enum_values = profile.sample_values
            if all(len(v) < 80 for v in enum_values):
                clause["enum"] = enum_values
                desc_parts.append("Low-cardinality categorical field inferred from observed data.")

    apply_domain_specific_overrides(profile, clause, desc_parts)

    if desc_parts:
        clause["description"] = " ".join(desc_parts)

    return clause


def infer_type(profile: ColumnProfile) -> str:
    # Conservative numeric inference:
    # only treat a column as numeric if the vast majority of sampled values
    # are genuinely numeric.
    if profile.stats is not None and column_is_mostly_numeric(profile):
        if looks_integer_like(profile):
            return "integer"
        return "number"

    dtype = profile.dtype.lower()
    if dtype in {"bool", "boolean"}:
        return "boolean"
    if dtype.startswith("datetime"):
        return "string"
    return "string"

def value_looks_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            parsed = float(text)
        except ValueError:
            return False
        return math.isfinite(parsed)
    return False


def column_is_mostly_numeric(profile: ColumnProfile) -> bool:
    # Use sample values as a safety gate. This prevents free-text columns
    # from being mislabeled as numeric just because profiling found some path.
    samples = profile.sample_values
    if not samples:
        return False

    numeric_like = sum(1 for v in samples if value_looks_numeric(v))
    ratio = numeric_like / max(len(samples), 1)

    # Require all or almost all sampled values to be numeric.
    return ratio >= 0.95


def looks_like_uuid_samples(samples: list[str]) -> bool:
    if not samples:
        return False
    return all(bool(UUID_RE.match(s.strip())) for s in samples[:5])

def looks_integer_like(profile: ColumnProfile) -> bool:
    stats = profile.stats
    if not stats:
        return False
    for key in ("min", "max", "p50"):
        value = stats.get(key)
        if value is None:
            return False
        if not float(value).is_integer():
            return False
    return True


def looks_like_datetime(samples: list[str]) -> bool:
    if not samples:
        return False
    return all(bool(ISO_8601_Z_RE.match(s)) for s in samples[:3])


def is_confidence_field(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith("confidence") or ".confidence" in lowered


def apply_domain_specific_overrides(profile: ColumnProfile, clause: dict[str, Any], desc_parts: list[str]) -> None:
    name = profile.name

    if name in {"overall_verdict", "entity.type", "run_type", "relationship", "type"}:
        enums: dict[str, list[str]] = {
            "overall_verdict": ["PASS", "FAIL", "WARN"],
            "entity.type": ["PERSON", "ORG", "LOCATION", "DATE", "AMOUNT", "OTHER"],
            "run_type": ["llm", "chain", "tool", "retriever", "embedding"],
            "relationship": ["IMPORTS", "CALLS", "READS", "WRITES", "PRODUCES", "CONSUMES"],
            "type": ["FILE", "TABLE", "SERVICE", "MODEL", "PIPELINE", "EXTERNAL"],
        }
        clause["type"] = "string"
        clause["enum"] = enums[name]
        desc_parts.append("Enumerated domain field with contract-bound legal values.")

    if name == "source_hash":
        clause["type"] = "string"
        clause["pattern"] = SHA256_RE.pattern
        desc_parts.append("SHA-256 hex digest of the source input.")

    if name == "rubric_version" or name == "schema_version":
        clause["type"] = "string"
        clause["pattern"] = SEMVER_RE.pattern
        desc_parts.append("Semantic version string.")

    if name == "git_commit":
        clause["type"] = "string"
        clause["pattern"] = GIT_SHA40_RE.pattern
        desc_parts.append("Full 40-character git commit SHA.")

    if name in {"event_type", "aggregate_type"}:
        clause["type"] = "string"
        clause["pattern"] = PASCAL_CASE_RE.pattern
        desc_parts.append("PascalCase identifier.")

    if name.endswith(".__len__"):
        clause["type"] = "integer"
        clause["minimum"] = 0
        desc_parts.append("Observed list length during profiling.")


def build_schema_section(profiles: dict[str, ColumnProfile]) -> dict[str, Any]:
    schema: dict[str, Any] = {}
    for col_name in sorted(profiles.keys()):
        schema[col_name] = infer_clause(profiles[col_name])
    return schema


def build_quality_checks(context: ContractContext, profiles: dict[str, ColumnProfile]) -> list[str]:
    checks: list[str] = []

    required_columns = [p for p in profiles.values() if p.null_fraction == 0.0]
    for profile in required_columns:
        checks.append(f"missing_count({profile.name}) = 0")

    for profile in profiles.values():
        if is_confidence_field(profile.name):
            checks.extend(
                [
                    f"min({profile.name}) >= 0.0",
                    f"max({profile.name}) <= 1.0",
                ]
            )
        if profile.name in {"overall_verdict", "entity.type", "run_type", "relationship", "type"}:
            checks.append(f"invalid_count({profile.name}) = 0")
        if profile.name == "doc_id":
            checks.append("duplicate_count(doc_id) = 0")
        if profile.name == "processing_time_ms":
            checks.append("min(processing_time_ms) > 0")
        if profile.name == "total_cost":
            checks.append("min(total_cost) >= 0")
        if profile.name == "total_tokens":
            checks.append("min(total_tokens) >= 0")

    checks.append("row_count >= 1")

    if context.logical_name == "traces":
        checks.append("end_time > start_time")
        checks.append("total_tokens = prompt_tokens + completion_tokens")

    return dedupe_preserve_order(checks)


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def inject_lineage(contract: dict[str, Any], lineage_snapshot: dict[str, Any] | None, context: ContractContext) -> dict[str, Any]:
    if lineage_snapshot is None:
        contract["lineage"] = {"upstream": [], "downstream": []}
        return contract

    nodes = lineage_snapshot.get("nodes", [])
    edges = lineage_snapshot.get("edges", [])

    source_hints = source_hints_for_context(context)
    downstream: list[dict[str, Any]] = []

    for edge in edges:
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        if any(hint in source.lower() for hint in source_hints):
            downstream.append(
                {
                    "id": target,
                    "description": f"Observed downstream consumer of {context.logical_name} from lineage snapshot.",
                    "fields_consumed": default_fields_consumed(context),
                }
            )

    if not downstream:
        # Fallback: infer from neighboring nodes if the lineage snapshot is sparse.
        for node in nodes:
            node_id = str(node.get("node_id", ""))
            if any(hint in node_id.lower() for hint in source_hints):
                downstream.append(
                    {
                        "id": node_id,
                        "description": f"Lineage-adjacent node related to {context.logical_name}.",
                        "fields_consumed": default_fields_consumed(context),
                    }
                )

    contract["lineage"] = {
        "upstream": [],
        "downstream": dedupe_downstream(downstream),
    }
    return contract


def source_hints_for_context(context: ContractContext) -> list[str]:
    mapping = {
        "extractions": ["week3", "extraction", "refinery"],
        "events": ["week5", "event"],
        "lineage_snapshots": ["week4", "lineage", "cartographer"],
        "traces": ["trace", "langsmith", "run"],
    }
    return mapping.get(context.logical_name, [context.logical_name.lower()])


def default_fields_consumed(context: ContractContext) -> list[str]:
    mapping = {
        "extractions": ["doc_id", "extracted_facts", "entities", "extraction_model"],
        "events": ["event_id", "event_type", "aggregate_id", "payload", "schema_version"],
        "lineage_snapshots": ["snapshot_id", "nodes", "edges"],
        "traces": ["id", "run_type", "inputs", "outputs", "total_tokens"],
    }
    return mapping.get(context.logical_name, [])


def dedupe_downstream(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id", ""))
        if item_id and item_id not in seen:
            seen.add(item_id)
            out.append(item)
    return out


def build_contract(context: ContractContext, profiles: dict[str, ColumnProfile], lineage_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "id": context.contract_id,
        "info": {
            "title": context.title,
            "version": "1.0.0",
            "owner": context.owner,
            "description": context.description,
        },
        "servers": {
            "local": {
                "type": "local",
                "path": context.source_path,
                "format": "jsonl",
            }
        },
        "terms": {
            "usage": "Internal inter-system data contract.",
            "limitations": "Auto-generated from observed data; review semantic clauses before production use.",
        },
        "schema": build_schema_section(profiles),
        "quality": {
            "type": "SodaChecks",
            "specification": {f"checks for {context.logical_name}": build_quality_checks(context, profiles)},
        },
    }

    add_llm_annotation_placeholder(contract, context, profiles)
    inject_lineage(contract, lineage_snapshot, context)
    return contract


def add_llm_annotation_placeholder(contract: dict[str, Any], context: ContractContext, profiles: dict[str, ColumnProfile]) -> None:
    ambiguous = [
        p.name
        for p in profiles.values()
        if p.dtype == "object" and p.cardinality_estimate > 10 and not p.name.endswith("_id")
    ]
    if ambiguous:
        contract["llm_annotations"] = [
            {
                "status": "pending",
                "fields": ambiguous[:10],
                "note": (
                    "Business-meaning enrichment can be added for ambiguous columns using a model-assisted pass. "
                    "This generator keeps baseline contracts deterministic and offline-safe."
                ),
            }
        ]


def build_dbt_schema(context: ContractContext, profiles: dict[str, ColumnProfile]) -> dict[str, Any]:
    model_name = Path(context.source_path).stem
    columns: list[dict[str, Any]] = []

    for profile in sorted(profiles.values(), key=lambda p: p.name):
        tests: list[Any] = []
        clause = infer_clause(profile)

        if clause.get("required"):
            tests.append("not_null")
        if profile.name == "doc_id":
            tests.append("unique")
        if "enum" in clause:
            tests.append({"accepted_values": {"values": clause["enum"]}})
        if clause.get("format") == "uuid":
            tests.append({"expect_uuid_format": {}})

        columns.append(
            {
                "name": profile.name,
                "description": clause.get("description", "Auto-generated column contract."),
                "tests": tests,
            }
        )

    return {
        "version": 2,
        "models": [
            {
                "name": model_name,
                "description": context.description,
                "columns": columns,
            }
        ],
    }


def output_paths(output_dir: str | Path, contract_id: str) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_name = sanitize_contract_basename(contract_id)
    contract_path = out_dir / f"{base_name}.yaml"
    dbt_path = out_dir / f"{base_name}_dbt.yml"
    return contract_path, dbt_path


def sanitize_contract_basename(contract_id: str) -> str:
    replacements = {
        "week3-document-refinery-extractions": "week3_extractions",
        "week5-event-records": "week5_events",
        "week4-lineage-snapshots": "week4_lineage",
        "langsmith-trace-records": "langsmith_traces",
    }
    return replacements.get(contract_id, contract_id.replace("-", "_"))


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)


def write_schema_snapshot(contract_path: Path, contract_id: str) -> Path:
    snapshot_dir = Path("schema_snapshots") / contract_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot_path = snapshot_dir / f"{timestamp}.yaml"
    shutil.copy2(contract_path, snapshot_path)
    return snapshot_path


def print_summary(
    source_path: str,
    contract_path: Path,
    dbt_path: Path,
    snapshot_path: Path,
    profiles: dict[str, ColumnProfile],
) -> None:
    print(f"[OK] source={source_path}")
    print(f"[OK] columns_profiled={len(profiles)}")
    print(f"[OK] contract={contract_path}")
    print(f"[OK] dbt_schema={dbt_path}")
    print(f"[OK] snapshot={snapshot_path}")


def main() -> None:
    args = parse_args()
    records = load_jsonl(args.source)
    lineage_snapshot = load_latest_lineage_snapshot(args.lineage)
    context = infer_context(args.contract_id, args.source, args.owner)

    df = flatten_records(records)
    profiles = build_column_profiles(df)

    contract = build_contract(context, profiles, lineage_snapshot)
    dbt_schema = build_dbt_schema(context, profiles)

    contract_path, dbt_path = output_paths(args.output, args.contract_id)
    write_yaml(contract_path, contract)
    write_yaml(dbt_path, dbt_schema)
    snapshot_path = write_schema_snapshot(contract_path, args.contract_id)

    print_summary(args.source, contract_path, dbt_path, snapshot_path, profiles)


if __name__ == "__main__":
    main()

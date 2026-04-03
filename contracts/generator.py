#!/usr/bin/env python3
"""ContractGenerator for TRP1 Week 8: Data Contract Enforcer.

Generates Bitol-compatible YAML contracts, dbt schema YAML, and timestamped
schema snapshots from JSONL outputs.

Rewritten goals:
- general nested JSONL profiling for any week
- path-based schema inference using dotted paths and [*] array selectors
- coverage-based required inference to avoid sparse-field false positives
- deterministic and evaluator-friendly CLI
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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
    observed_count: int
    record_count: int
    stats: dict[str, float] | None = None
    dominant_pattern: str | None = None
    presence_by_record: list[bool] = field(default_factory=list)


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
    return records[-1] if records else None


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


def safe_string(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def singularize(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s") and len(name) > 1:
        return name[:-1]
    return name

def base_array_path(name: str) -> str | None:
    if name.endswith("[*]"):
        return name[:-3]
    if "[*]." in name:
        return name.split("[*].", 1)[0]
    return None


def profile_record_paths(record: dict[str, Any], prefix: str = "") -> dict[str, list[Any]]:
    """Profile one JSON record into schema paths.

    Examples:
    - payload.agent_id
    - metadata.source_service
    - extracted_facts.__len__
    - extracted_facts[*].confidence
    """
    out: dict[str, list[Any]] = defaultdict(list)

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if path:
                out[path].append(value)
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                walk(child, child_path)
            return

        if isinstance(value, list):
            if path:
                out[path].append(value)
                out[f"{path}.__len__"].append(len(value))

            if not value:
                return

            scalar_only = all(not isinstance(item, (dict, list)) for item in value)
            if scalar_only:
                for item in value:
                    out[f"{path}[*]"].append(item)
                return

            for item in value:
                walk(item, f"{path}[*]")
            return

        out[path].append(value)

    walk(record, prefix)
    return out


def infer_required(record_count: int, observed_count: int, threshold: float = 0.98) -> bool:
    if record_count == 0:
        return False
    return (observed_count / record_count) >= threshold


def infer_value_dtype(values: list[Any]) -> str:
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "unknown"

    if all(isinstance(v, bool) for v in non_null):
        return "boolean"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in non_null):
        return "integer"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return "number"
    if all(isinstance(v, dict) for v in non_null):
        return "object"
    if all(isinstance(v, list) for v in non_null):
        return "array"
    return "string"


def dominant_character_pattern(values: list[Any]) -> str | None:
    texts = [str(v) for v in values if v is not None][:200]
    if not texts:
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

    counts = Counter(classify(v) for v in texts)
    return counts.most_common(1)[0][0]


def numeric_stats(values: list[Any]) -> dict[str, float] | None:
    clean: list[float] = []
    non_null_values = [v for v in values if v is not None]

    for value in non_null_values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            parsed = float(value)
            if math.isfinite(parsed):
                clean.append(parsed)
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
                clean.append(parsed)

    if not clean:
        return None

    ratio = len(clean) / max(len(non_null_values), 1)
    if ratio < 0.95:
        return None

    clean_sorted = sorted(clean)
    count = len(clean_sorted)

    def quantile(q: float) -> float:
        if count == 1:
            return clean_sorted[0]
        idx = q * (count - 1)
        lower = math.floor(idx)
        upper = math.ceil(idx)
        if lower == upper:
            return clean_sorted[lower]
        frac = idx - lower
        return clean_sorted[lower] * (1 - frac) + clean_sorted[upper] * frac

    mean = sum(clean_sorted) / count
    variance = sum((x - mean) ** 2 for x in clean_sorted) / count
    return {
        "min": float(clean_sorted[0]),
        "max": float(clean_sorted[-1]),
        "mean": float(mean),
        "p25": float(quantile(0.25)),
        "p50": float(quantile(0.50)),
        "p75": float(quantile(0.75)),
        "p95": float(quantile(0.95)),
        "p99": float(quantile(0.99)),
        "stddev": float(math.sqrt(variance)),
    }


def build_column_profiles(records: list[dict[str, Any]]) -> dict[str, ColumnProfile]:
    all_values: dict[str, list[Any]] = defaultdict(list)
    presence_map: dict[str, list[bool]] = defaultdict(list)

    per_record_paths: list[dict[str, list[Any]]] = [profile_record_paths(record) for record in records]
    all_paths = sorted({path for record_paths in per_record_paths for path in record_paths.keys()})

    for path in all_paths:
        for record_paths in per_record_paths:
            if path in record_paths:
                vals = record_paths[path]
                presence_map[path].append(True)
                if vals:
                    all_values[path].extend(vals)
                else:
                    all_values[path].append(None)
            else:
                presence_map[path].append(False)

    profiles: dict[str, ColumnProfile] = {}
    record_count = len(records)

    for path in all_paths:
        values = all_values[path]
        presence_by_record = presence_map[path]
        observed_count = sum(1 for present in presence_by_record if present)
        null_count = sum(1 for v in values if v is None)
        non_null_values = [v for v in values if v is not None]
        sample_values = [safe_string(v) for v in non_null_values[:5]]
        cardinality_estimate = len({safe_string(v) for v in non_null_values})

        profile = ColumnProfile(
            name=path,
            path=path,
            dtype=infer_value_dtype(non_null_values),
            null_fraction=(null_count / len(values)) if values else 1.0,
            cardinality_estimate=cardinality_estimate,
            sample_values=sample_values,
            observed_count=observed_count,
            record_count=record_count,
            stats=numeric_stats(values),
            dominant_pattern=dominant_character_pattern(non_null_values),
            presence_by_record=presence_by_record,
        )
        profiles[path] = profile

    return profiles


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
    samples = profile.sample_values
    if not samples:
        return False
    numeric_like = sum(1 for v in samples if value_looks_numeric(v))
    return (numeric_like / max(len(samples), 1)) >= 0.95


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
        if value is None or not float(value).is_integer():
            return False
    return True


def looks_like_datetime(samples: list[str]) -> bool:
    if not samples:
        return False
    return all(bool(ISO_8601_Z_RE.match(s.strip())) for s in samples[:3])


def is_confidence_field(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith("confidence") or ".confidence" in lowered


def infer_type(profile: ColumnProfile) -> str:
    name = profile.name

    if name.endswith(".__len__"):
        return "integer"

    if name.endswith("[*]"):
        if profile.dtype == "object":
            return "object"
        if profile.dtype == "boolean":
            return "boolean"
        if profile.stats is not None and column_is_mostly_numeric(profile):
            if looks_integer_like(profile):
                return "integer"
            return "number"
        return "string"

    if profile.dtype == "array":
        return "array"
    if profile.dtype == "boolean":
        return "boolean"
    if profile.dtype == "object":
        return "object"

    if profile.stats is not None and column_is_mostly_numeric(profile):
        if looks_integer_like(profile):
            return "integer"
        return "number"

    return "string"


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

    if name in {"rubric_version", "schema_version"}:
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

def infer_clause(profile: ColumnProfile) -> dict[str, Any]:
    clause: dict[str, Any] = {
        "type": infer_type(profile),
        "required": infer_required(profile.record_count, profile.observed_count),
    }

    desc_parts: list[str] = []

    if profile.name.endswith("_id") or profile.name.endswith(".id"):
        if looks_like_uuid_samples(profile.sample_values):
            clause["format"] = "uuid"
            desc_parts.append("Identifier field; UUID format verified from observed values.")
        else:
            desc_parts.append("Identifier field; observed values are not UUID-formatted.")

    if (
        profile.name.endswith("_at")
        or profile.name.endswith("_time")
        or profile.name in {"start_time", "end_time"}
    ):
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

    if profile.stats is not None and (
        profile.name.endswith("processing_time_ms") or profile.name.endswith(".processing_time_ms")
    ):
        clause["type"] = "integer"
        clause["minimum"] = 1
        desc_parts.append("Positive processing latency in milliseconds.")

    if profile.stats is not None and (
        profile.name.endswith("total_cost") or profile.name.endswith("total_cost_usd")
    ):
        clause["type"] = "number"
        clause["minimum"] = 0.0
        desc_parts.append("Non-negative cost in USD.")

    if profile.stats is not None and profile.name.endswith("sequence_number"):
        clause["type"] = "integer"
        clause["minimum"] = 1
        desc_parts.append("Monotonic sequence number per aggregate.")

    if (
        clause["type"] == "string"
        and not profile.name.endswith("[*]")
        and "[*]." not in profile.name
        and not profile.name.endswith(".__len__")
        and 0 < profile.cardinality_estimate <= 10
    ):
        enum_values = list(dict.fromkeys(profile.sample_values))
        if (
            len(enum_values) == profile.cardinality_estimate
            and all(len(v) < 80 for v in enum_values)
            and all(not v.startswith("[") for v in enum_values)
            and all(not v.startswith("{") for v in enum_values)
        ):
            clause["enum"] = enum_values
            desc_parts.append("Low-cardinality categorical field inferred from observed data.")

    apply_domain_specific_overrides(profile, clause, desc_parts)

    if desc_parts:
        clause["description"] = " ".join(desc_parts)

    return clause

def build_schema_section(profiles: dict[str, ColumnProfile]) -> dict[str, Any]:
    schema = {name: infer_clause(profile) for name, profile in sorted(profiles.items())}

    for name in list(schema.keys()):
        array_base = base_array_path(name)
        if array_base and array_base in schema:
            item_clause = schema[name]
            parent_clause = schema[array_base]

            if parent_clause.get("type") == "array":
                item_type = item_clause.get("type")
                if item_type in {"string", "integer", "number", "boolean", "object"}:
                    parent_clause["items"] = {"type": item_type}

                    if "enum" in item_clause and item_type == "string":
                        parent_clause["items"]["enum"] = item_clause["enum"]

    return schema

def build_quality_checks(context: ContractContext, profiles: dict[str, ColumnProfile]) -> list[str]:
    checks: list[str] = []

    for profile in profiles.values():
        clause = infer_clause(profile)
        if clause.get("required"):
            checks.append(f"missing_count({profile.name}) = 0")

        if is_confidence_field(profile.name):
            checks.append(f"min({profile.name}) >= 0.0")
            checks.append(f"max({profile.name}) <= 1.0")

        if profile.name in {"overall_verdict", "entity.type", "run_type", "relationship", "type"}:
            checks.append(f"invalid_count({profile.name}) = 0")

        if profile.name in {"doc_id", "document_id", "event_id", "trace_id", "snapshot_id"}:
            checks.append(f"duplicate_count({profile.name}) = 0")

        if profile.name == "processing_time_ms":
            checks.append("min(processing_time_ms) > 0")

        if profile.name in {"total_cost", "total_cost_usd"}:
            checks.append(f"min({profile.name}) >= 0")

        if profile.name == "total_tokens":
            checks.append("min(total_tokens) >= 0")

    checks.append("row_count >= 1")

    if context.logical_name == "traces":
        checks.append("end_time > start_time")
        checks.append("total_tokens = prompt_tokens + completion_tokens")

    if context.logical_name == "events":
        checks.append("recorded_at >= occurred_at")
        checks.append("sequence_number is_monotonic_per aggregate_id")

    return dedupe_preserve_order(checks)


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


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


def inject_lineage(
    contract: dict[str, Any],
    lineage_snapshot: dict[str, Any] | None,
    context: ContractContext,
) -> dict[str, Any]:
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


def add_llm_annotation_placeholder(
    contract: dict[str, Any],
    context: ContractContext,
    profiles: dict[str, ColumnProfile],
) -> None:
    ambiguous = [
        p.name
        for p in profiles.values()
        if p.dtype in {"object", "array"} and p.cardinality_estimate > 10 and not p.name.endswith("_id")
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


def build_contract(
    context: ContractContext,
    profiles: dict[str, ColumnProfile],
    lineage_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
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


def build_dbt_schema(context: ContractContext, profiles: dict[str, ColumnProfile]) -> dict[str, Any]:
    model_name = Path(context.source_path).stem
    columns: list[dict[str, Any]] = []

    for profile in sorted(profiles.values(), key=lambda p: p.name):
        tests: list[Any] = []
        clause = infer_clause(profile)
        name = profile.name

        if clause.get("required"):
            tests.append("not_null")

        important_not_null = {
            "doc_id",
            "document_id",
            "event_id",
            "event_type",
            "aggregate_id",
            "aggregate_type",
            "sequence_number",
            "recorded_at",
            "occurred_at",
            "trace_id",
            "snapshot_id",
        }
        if name in important_not_null and "not_null" not in tests:
            tests.append("not_null")

        unique_candidates = {"doc_id", "document_id", "event_id", "trace_id", "snapshot_id"}
        if name in unique_candidates:
            tests.append("unique")

        if "enum" in clause and clause["enum"]:
            tests.append({"accepted_values": {"values": clause["enum"]}})

        if clause.get("format") == "uuid":
            tests.append({"expect_uuid_format": {}})

        if clause.get("format") == "date-time":
            tests.append({"expect_datetime_format": {}})

        if "pattern" in clause:
            tests.append({"expect_column_values_to_match_regex": {"regex": clause["pattern"]}})

        if "minimum" in clause:
            tests.append({"dbt_utils.expression_is_true": {"expression": f"{name} >= {clause['minimum']}"}})

        if "maximum" in clause:
            tests.append({"dbt_utils.expression_is_true": {"expression": f"{name} <= {clause['maximum']}"}})

        if context.logical_name == "events":
            if name == "event_type":
                tests.append({"expect_column_values_to_match_regex": {"regex": PASCAL_CASE_RE.pattern}})
            if name == "aggregate_type":
                tests.append({"expect_column_values_to_match_regex": {"regex": PASCAL_CASE_RE.pattern}})
            if name == "sequence_number":
                tests.append({"dbt_utils.expression_is_true": {"expression": "sequence_number >= 1"}})
            if name == "aggregate_id":
                tests.append(
                    {
                        "relationships": {
                            "to": "ref('event_streams')",
                            "field": "stream_id",
                        }
                    }
                )

        if context.logical_name == "extractions":
            if name.endswith("confidence") or name.endswith(".confidence"):
                tests.append({"dbt_utils.expression_is_true": {"expression": f"{name} >= 0.0"}})
                tests.append({"dbt_utils.expression_is_true": {"expression": f"{name} <= 1.0"}})

        deduped_tests: list[Any] = []
        seen = set()
        for test in tests:
            key = json.dumps(test, sort_keys=True) if isinstance(test, dict) else str(test)
            if key not in seen:
                seen.add(key)
                deduped_tests.append(test)

        col_entry = {
            "name": name,
            "description": clause.get("description", "Auto-generated column contract."),
        }
        if deduped_tests:
            col_entry["tests"] = deduped_tests

        columns.append(col_entry)

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


def sanitize_contract_basename(contract_id: str) -> str:
    replacements = {
        "week3-document-refinery-extractions": "week3_extractions",
        "week5-event-records": "week5_events",
        "week4-lineage-snapshots": "week4_lineage",
        "langsmith-trace-records": "langsmith_traces",
    }
    return replacements.get(contract_id, contract_id.replace("-", "_"))


def output_paths(output_dir: str | Path, contract_id: str) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_name = sanitize_contract_basename(contract_id)
    return out_dir / f"{base_name}.yaml", out_dir / f"{base_name}_dbt.yml"


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

    profiles = build_column_profiles(records)
    contract = build_contract(context, profiles, lineage_snapshot)
    dbt_schema = build_dbt_schema(context, profiles)

    contract_path, dbt_path = output_paths(args.output, args.contract_id)
    write_yaml(contract_path, contract)
    write_yaml(dbt_path, dbt_schema)
    snapshot_path = write_schema_snapshot(contract_path, args.contract_id)

    print_summary(args.source, contract_path, dbt_path, snapshot_path, profiles)


if __name__ == "__main__":
    main()

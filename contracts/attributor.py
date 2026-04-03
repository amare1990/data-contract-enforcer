#!/usr/bin/env python3
"""ViolationAttributor for the Data Contract Enforcer.

Consumes a validation report, a lineage snapshot, a generated contract, and an
optional contract registry. The registry is treated as the authoritative blast
radius source; lineage is used as enrichment for transitive contamination and
upstream file discovery.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Attribute validation failures to likely upstream commits")
    parser.add_argument("--violation", required=True, help="Path to validation report JSON")
    parser.add_argument("--lineage", required=True, help="Path to lineage snapshot JSONL")
    parser.add_argument("--contract", required=True, help="Path to generated contract YAML")
    parser.add_argument("--output", required=True, help="Path to violations.jsonl output")
    parser.add_argument(
        "--registry",
        default="contract_registry/subscriptions.yaml",
        help="Path to contract registry subscriptions YAML (default: contract_registry/subscriptions.yaml)",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Git repository root to use for git log / git blame (default: current directory)",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=5,
        help="Maximum blame candidates per violation (default: 5)",
    )
    return parser.parse_args()


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path} line {line_no}: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"Expected JSON object in {path} line {line_no}")
            records.append(obj)
    if not records:
        raise ValueError(f"No JSON objects found in {path}")
    return records


def load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    return payload


def load_registry(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"subscriptions": []}
    return load_yaml(p)


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_field_path(value: str) -> str:
    normalized = value.strip()
    normalized = normalized.replace("[*].", ".")
    normalized = normalized.replace("[*]", "")
    normalized = normalized.replace(".__len__", "")
    normalized = re.sub(r"\.+", ".", normalized)
    return normalized.strip(".")


def infer_system_hint(check_id: str, contract_id: str) -> str:
    text = f"{check_id} {contract_id}".lower()
    if "week1" in text:
        return "week1"
    if "week2" in text or "verdict" in text:
        return "week2"
    if "week3" in text or "extraction" in text or "refinery" in text:
        return "week3"
    if "week4" in text or "lineage" in text or "cartographer" in text:
        return "week4"
    if "week5" in text or "event" in text:
        return "week5"
    if "trace" in text or "langsmith" in text:
        return "trace"
    return contract_id.split("-")[0].lower()


def build_graph(
    lineage_snapshot: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, list[str]]]:
    nodes = {str(node.get("node_id")): node for node in lineage_snapshot.get("nodes", []) if node.get("node_id")}
    reverse_adj: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    forward_adj: dict[str, list[str]] = {node_id: [] for node_id in nodes}

    for edge in lineage_snapshot.get("edges", []):
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        if source and target:
            forward_adj.setdefault(source, []).append(target)
            reverse_adj.setdefault(target, []).append(source)
            reverse_adj.setdefault(source, reverse_adj.get(source, []))
            forward_adj.setdefault(target, forward_adj.get(target, []))

    return nodes, reverse_adj, forward_adj


def choose_start_nodes(nodes: dict[str, dict[str, Any]], system_hint: str, column_name: str) -> list[str]:
    candidates: list[str] = []
    lowered_col = normalize_field_path(column_name).lower()
    for node_id, node in nodes.items():
        nid = node_id.lower()
        label = str(node.get("label", "")).lower()
        path = str(node.get("metadata", {}).get("path", "")).lower()
        haystack = f"{nid} {label} {path}"
        if system_hint in haystack or any(tok for tok in lowered_col.split(".") if tok and tok in haystack):
            candidates.append(node_id)
    return candidates


def bfs_upstream_files(
    start_nodes: list[str],
    nodes: dict[str, dict[str, Any]],
    reverse_adj: dict[str, list[str]],
    max_results: int = 5,
) -> list[dict[str, Any]]:
    if not start_nodes:
        return []

    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque((node_id, 0) for node_id in start_nodes)
    results: list[dict[str, Any]] = []

    while queue and len(results) < max_results:
        node_id, dist = queue.popleft()
        if node_id in visited:
            continue
        visited.add(node_id)

        node = nodes.get(node_id, {})
        metadata = node.get("metadata", {}) if isinstance(node, dict) else {}
        node_type = str(node.get("type", ""))
        path = metadata.get("path")

        if node_type == "FILE" and path:
            results.append({
                "node_id": node_id,
                "file_path": str(path),
                "lineage_distance": dist,
            })

        for upstream in reverse_adj.get(node_id, []):
            if upstream not in visited:
                queue.append((upstream, dist + 1))

    return results


def fallback_file_candidates(nodes: dict[str, dict[str, Any]], system_hint: str, max_results: int = 5) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        if str(node.get("type")) != "FILE":
            continue
        metadata = node.get("metadata", {})
        path = str(metadata.get("path", ""))
        haystack = f"{node_id} {path}".lower()
        if system_hint in haystack:
            out.append({"node_id": node_id, "file_path": path, "lineage_distance": 1})
        if len(out) >= max_results:
            break
    return out


def parse_git_timestamp(value: str) -> datetime:
    cleaned = value.strip()
    return datetime.fromisoformat(cleaned.replace(" +", "+").replace(" -", "-"))


def run_git_log(repo_root: str | Path, file_path: str, days: int = 14) -> list[dict[str, Any]]:
    cmd = [
        "git",
        "log",
        "--follow",
        f"--since={days} days ago",
        "--format=%H|%an|%ae|%ai|%s",
        "--",
        file_path,
    ]
    result = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, check=False)
    commits: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if "|" not in line:
            continue
        commit_hash, author_name, author_email, ts, msg = line.split("|", 4)
        commits.append(
            {
                "commit_hash": commit_hash,
                "author": f"{author_name} <{author_email}>",
                "commit_timestamp": ts.strip(),
                "commit_message": msg.strip(),
            }
        )
    return commits


def score_commit(commit_timestamp: str, detected_at: str, lineage_distance: int) -> float:
    detected_dt = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
    commit_dt = parse_git_timestamp(commit_timestamp)
    days_since_commit = abs((detected_dt - commit_dt).days)
    score = 1.0 - (days_since_commit * 0.1) - (lineage_distance * 0.2)
    return round(max(score, 0.0), 3)


def ensure_candidate_floor(
    file_candidates: list[dict[str, Any]],
    repo_root: str | Path,
    detected_at: str,
    max_candidates: int,
) -> list[dict[str, Any]]:
    blame_chain: list[dict[str, Any]] = []

    for candidate in file_candidates:
        commits = run_git_log(repo_root, candidate["file_path"])
        if not commits:
            blame_chain.append(
                {
                    "rank": len(blame_chain) + 1,
                    "file_path": candidate["file_path"],
                    "commit_hash": "UNKNOWN",
                    "author": "UNKNOWN",
                    "commit_timestamp": detected_at,
                    "commit_message": "No recent git history found for file.",
                    "confidence_score": round(max(0.0, 0.4 - candidate["lineage_distance"] * 0.1), 3),
                }
            )
        else:
            for commit in commits[:max_candidates]:
                blame_chain.append(
                    {
                        "rank": len(blame_chain) + 1,
                        "file_path": candidate["file_path"],
                        "commit_hash": commit["commit_hash"],
                        "author": commit["author"],
                        "commit_timestamp": commit["commit_timestamp"],
                        "commit_message": commit["commit_message"],
                        "confidence_score": score_commit(
                            commit["commit_timestamp"],
                            detected_at,
                            candidate["lineage_distance"],
                        ),
                    }
                )
                if len(blame_chain) >= max_candidates:
                    break
        if len(blame_chain) >= max_candidates:
            break

    if not blame_chain:
        blame_chain.append(
            {
                "rank": 1,
                "file_path": "UNKNOWN",
                "commit_hash": "UNKNOWN",
                "author": "UNKNOWN",
                "commit_timestamp": detected_at,
                "commit_message": "No lineage or git candidate available.",
                "confidence_score": 0.0,
            }
        )

    blame_chain = sorted(blame_chain, key=lambda x: x["confidence_score"], reverse=True)[:max_candidates]
    for idx, item in enumerate(blame_chain, start=1):
        item["rank"] = idx
    return blame_chain


def load_latest_lineage_snapshot(path: str | Path) -> dict[str, Any]:
    return load_jsonl(path)[-1]


def contract_downstream(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return contract.get("lineage", {}).get("downstream", []) or []


def normalize_breaking_fields(subscription: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in subscription.get("breaking_fields", []) or []:
        if isinstance(item, str):
            normalized.append({"field": normalize_field_path(item), "reason": ""})
        elif isinstance(item, dict):
            normalized.append(
                {
                    "field": normalize_field_path(str(item.get("field", ""))),
                    "reason": str(item.get("reason", "")),
                }
            )
    return [item for item in normalized if item["field"]]


def query_registry(
    registry: dict[str, Any],
    contract_id: str,
    breaking_field: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    normalized_breaking_field = normalize_field_path(breaking_field)

    for raw_sub in registry.get("subscriptions", []) or []:
        if not isinstance(raw_sub, dict):
            continue
        if str(raw_sub.get("contract_id", "")) != contract_id:
            continue
        breaking_fields = normalize_breaking_fields(raw_sub)
        if not breaking_fields:
            continue
        reasons = [
            item
            for item in breaking_fields
            if item["field"] == normalized_breaking_field
            or item["field"].endswith(f".{normalized_breaking_field}")
            or normalized_breaking_field.endswith(f".{item["field"]}")
        ]
        if reasons:
            enriched = dict(raw_sub)
            enriched["breaking_fields"] = breaking_fields
            enriched["matched_breaking_field"] = normalized_breaking_field
            enriched["match_reason"] = reasons[0].get("reason", "")
            matches.append(enriched)

    return matches


def fallback_blast_radius_from_contract(contract: dict[str, Any], records_failing: int, breaking_field: str) -> dict[str, Any]:
    downstream = contract_downstream(contract)
    return {
        "source": "contract_lineage",
        "breaking_field": normalize_field_path(breaking_field),
        "affected_subscribers": [
            {
                "subscriber_id": d.get("id"),
                "subscriber_team": d.get("subscriber_team"),
                "fields_consumed": d.get("fields_consumed", []),
                "validation_mode": d.get("validation_mode"),
                "contact": d.get("contact"),
                "contamination_depth": 0,
            }
            for d in downstream
            if d.get("id")
        ],
        "affected_nodes": [d.get("id") for d in downstream if d.get("id")],
        "affected_pipelines": [d.get("id") for d in downstream if "pipeline" in str(d.get("id", "")).lower()],
        "estimated_records": records_failing,
        "registry_match_count": 0,
    }


def choose_subscriber_nodes(nodes: dict[str, dict[str, Any]], subscriber_id: str) -> list[str]:
    target = subscriber_id.lower()
    tokens = [tok for tok in re.split(r"[-_]+", target) if tok and tok != "team"]
    matches: list[str] = []
    for node_id, node in nodes.items():
        haystack = " ".join(
            [
                node_id.lower(),
                str(node.get("label", "")).lower(),
                str(node.get("metadata", {}).get("path", "")).lower(),
                str(node.get("metadata", {}).get("purpose", "")).lower(),
            ]
        )
        if target in haystack or sum(tok in haystack for tok in tokens) >= min(2, len(tokens)):
            matches.append(node_id)
    return matches[:5]


def enrich_with_lineage(
    blast_radius: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    forward_adj: dict[str, list[str]],
) -> dict[str, Any]:
    affected_nodes: set[str] = set(blast_radius.get("affected_nodes", []))
    contamination_depths: dict[str, int] = {}

    for subscriber in blast_radius.get("affected_subscribers", []):
        seed_nodes = choose_subscriber_nodes(nodes, str(subscriber.get("subscriber_id", "")))
        if not seed_nodes:
            subscriber["contamination_depth"] = 0
            continue

        queue: deque[tuple[str, int]] = deque((node_id, 0) for node_id in seed_nodes)
        visited: set[str] = set()
        max_depth = 0
        while queue:
            node_id, depth = queue.popleft()
            if node_id in visited:
                continue
            visited.add(node_id)
            max_depth = max(max_depth, depth)
            affected_nodes.add(node_id)
            contamination_depths[node_id] = min(depth, contamination_depths.get(node_id, depth))
            for downstream in forward_adj.get(node_id, []):
                if downstream not in visited:
                    queue.append((downstream, depth + 1))
        subscriber["contamination_depth"] = max_depth

    blast_radius["affected_nodes"] = sorted(affected_nodes)
    blast_radius["contamination_depth"] = max(contamination_depths.values(), default=0)
    blast_radius["affected_pipelines"] = sorted(
        node_id for node_id in affected_nodes if "pipeline" in node_id.lower()
    )
    return blast_radius


def build_blast_radius(
    contract: dict[str, Any],
    contract_id: str,
    failure: dict[str, Any],
    registry: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    forward_adj: dict[str, list[str]],
) -> dict[str, Any]:
    records_failing = int(failure.get("records_failing", 0) or 0)
    breaking_field = normalize_field_path(str(failure.get("column_name", "unknown_field")))
    matches = query_registry(registry, contract_id, breaking_field)

    if matches:
        blast_radius = {
            "source": "registry",
            "breaking_field": breaking_field,
            "affected_subscribers": [
                {
                    "subscriber_id": sub.get("subscriber_id"),
                    "subscriber_team": sub.get("subscriber_team"),
                    "fields_consumed": sub.get("fields_consumed", []),
                    "validation_mode": sub.get("validation_mode"),
                    "contact": sub.get("contact"),
                    "reason": sub.get("match_reason", ""),
                }
                for sub in matches
            ],
            "affected_nodes": [],
            "affected_pipelines": [],
            "estimated_records": records_failing,
            "registry_match_count": len(matches),
        }
    else:
        blast_radius = fallback_blast_radius_from_contract(contract, records_failing, breaking_field)

    return enrich_with_lineage(blast_radius, nodes, forward_adj)


def select_failures(validation_report: dict[str, Any]) -> list[dict[str, Any]]:
    return [result for result in validation_report.get("results", []) if result.get("status") == "FAIL"]


def attribute_failure(
    failure: dict[str, Any],
    validation_report: dict[str, Any],
    lineage_snapshot: dict[str, Any],
    contract: dict[str, Any],
    registry: dict[str, Any],
    repo_root: str | Path,
    max_candidates: int,
) -> dict[str, Any]:
    contract_id = validation_report.get("contract_id", contract.get("id", "unknown_contract"))
    check_id = str(failure.get("check_id", "unknown_check"))
    detected_at = validation_report.get("run_timestamp") or iso_now()
    column_name = str(failure.get("column_name", "unknown_column"))
    system_hint = infer_system_hint(check_id, contract_id)

    nodes, reverse_adj, forward_adj = build_graph(lineage_snapshot)
    start_nodes = choose_start_nodes(nodes, system_hint, column_name)
    file_candidates = bfs_upstream_files(start_nodes, nodes, reverse_adj, max_results=max_candidates)
    if not file_candidates:
        file_candidates = fallback_file_candidates(nodes, system_hint, max_results=max_candidates)

    blame_chain = ensure_candidate_floor(file_candidates, repo_root, detected_at, max_candidates)

    return {
        "violation_id": str(uuid.uuid4()),
        "contract_id": contract_id,
        "check_id": check_id,
        "failing_field": normalize_field_path(column_name),
        "detected_at": detected_at,
        "blame_chain": blame_chain,
        "blast_radius": build_blast_radius(contract, contract_id, failure, registry, nodes, forward_adj),
    }


def write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    args = parse_args()
    validation_report = load_json(args.violation)
    lineage_snapshot = load_latest_lineage_snapshot(args.lineage)
    contract = load_yaml(args.contract)
    registry = load_registry(args.registry)

    failures = select_failures(validation_report)
    attributed: list[dict[str, Any]] = []
    for failure in failures:
        attributed.append(
            attribute_failure(
                failure,
                validation_report,
                lineage_snapshot,
                contract,
                registry,
                args.repo_root,
                args.max_candidates,
            )
        )

    if not attributed:
        print("[OK] no FAIL results found in validation report; nothing to attribute")
        return

    write_jsonl(args.output, attributed)
    print(f"[OK] failures_attributed={len(attributed)}")
    print(f"[OK] registry_path={args.registry}")
    print(f"[OK] output={args.output}")


if __name__ == "__main__":
    main()

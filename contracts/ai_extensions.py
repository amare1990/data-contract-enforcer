#!/usr/bin/env python3
"""AI Contract Extensions for TRP1 Week 8: Data Contract Enforcer.

Implements three AI-specific contract checks:
1. Embedding drift detection
2. Prompt input schema validation
3. Structured LLM output schema violation rate

Supports:
- Week 3 extraction records
- Week 5 event records
- LangSmith-like trace exports
- Week 2 verdict records for structured output validation

Enhancements:
- writes WARN entries to violation_log/violations.jsonl when violation rates exceed thresholds
- includes contract_id, metric values, and timestamp in violation log entries
- makes LangSmith / trace-oriented inputs explicit in contract inference
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from jsonschema import ValidationError, validate

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


PROMPT_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["doc_id", "source_path", "content_preview"],
    "properties": {
        "doc_id": {"type": "string", "minLength": 1},
        "source_path": {"type": "string", "minLength": 1},
        "content_preview": {"type": "string", "minLength": 1, "maxLength": 8000},
    },
    "additionalProperties": True,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AI-specific data contract extensions")
    parser.add_argument(
        "--mode",
        default="all",
        choices=["all", "embedding", "prompt", "output"],
        help="Which AI checks to run (default: all)",
    )
    parser.add_argument(
        "--extractions",
        default=None,
        help="Path to source JSONL for embedding/prompt checks",
    )
    parser.add_argument(
        "--verdicts",
        default=None,
        help="Path to Week 2 verdicts JSONL",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output JSON path for AI extension results",
    )
    parser.add_argument(
        "--embedding-model",
        default="text-embedding-3-small",
        help="Embedding model name",
    )
    parser.add_argument(
        "--embedding-threshold",
        type=float,
        default=0.15,
        help="Cosine-distance threshold for embedding drift failure",
    )
    parser.add_argument(
        "--warn-threshold",
        type=float,
        default=0.02,
        help="Violation-rate threshold for structured output WARN",
    )
    parser.add_argument(
        "--baseline-rate",
        type=float,
        default=None,
        help="Optional baseline violation rate for trend classification",
    )
    parser.add_argument(
        "--contract-id",
        default=None,
        help="Optional explicit contract identifier used in violation-log entries",
    )
    parser.add_argument(
        "--violation-log",
        default=None,
        help="Optional override path. If not provided, uses violation_log/<contract_id>_violations.jsonl",
    )
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
    sim = float(np.dot(a, b) / denom)
    return 1.0 - sim


def deterministic_local_embedding(text: str, dim: int = 64) -> np.ndarray:
    vec = np.zeros(dim, dtype=np.float64)
    for token in text.lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


def embed_texts(texts: list[str], model: str) -> np.ndarray:
    sample = [t for t in texts if t][:200]
    if not sample:
        return np.zeros((0, 64), dtype=np.float64)

    api_key = os.environ.get("OPENAI_API_KEY")

    if OpenAI is not None and api_key:
        client = OpenAI()
        response = client.embeddings.create(input=sample, model=model)
        return np.array([item.embedding for item in response.data], dtype=np.float64)

    return np.array([deterministic_local_embedding(text) for text in sample], dtype=np.float64)


def extract_texts(records: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []

    for record in records:
        direct_text = record.get("text")
        if isinstance(direct_text, str) and direct_text.strip():
            texts.append(direct_text.strip())

        extracted_facts = record.get("extracted_facts") or []
        if isinstance(extracted_facts, list):
            for fact in extracted_facts:
                if not isinstance(fact, dict):
                    continue
                fact_text = fact.get("text")
                if isinstance(fact_text, str) and fact_text.strip():
                    texts.append(fact_text.strip())

        payload = record.get("payload")
        if isinstance(payload, dict):
            for key in (
                "output_summary",
                "tool_input_summary",
                "tool_output_summary",
                "remediation_description",
                "override_reason",
                "input",
                "output",
            ):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())

        inputs = record.get("inputs")
        if isinstance(inputs, dict):
            for value in inputs.values():
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())

        outputs = record.get("outputs")
        if isinstance(outputs, dict):
            for value in outputs.values():
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())

    return texts


def embedding_baseline_path(extractions_path: str | Path) -> Path:
    stem = Path(extractions_path).stem
    return Path("schema_snapshots") / f"embedding_baseline_{stem}.npz"


def check_embedding_drift(
    texts: list[str],
    baseline_path: str | Path,
    threshold: float = 0.15,
    model: str = "text-embedding-3-small",
) -> dict[str, Any]:
    vecs = embed_texts(texts, model=model)
    if len(vecs) == 0:
        return {
            "status": "ERROR",
            "drift_score": None,
            "threshold": threshold,
            "sample_size": 0,
            "interpretation": "No text values available for embedding drift analysis.",
            "backend": "openai" if (OpenAI is not None and os.environ.get("OPENAI_API_KEY")) else "local-hash",
        }

    centroid = vecs.mean(axis=0)
    baseline_path = Path(baseline_path)
    ensure_parent(baseline_path)

    backend = "openai" if (OpenAI is not None and os.environ.get("OPENAI_API_KEY")) else "local-hash"

    if not baseline_path.exists():
        np.savez(baseline_path, centroid=centroid)
        return {
            "status": "BASELINE_SET",
            "drift_score": 0.0,
            "threshold": threshold,
            "sample_size": int(len(vecs)),
            "interpretation": "Baseline created from current embedding centroid.",
            "backend": backend,
        }

    baseline = np.load(baseline_path)["centroid"]
    drift = round(float(cosine_distance(centroid, baseline)), 4)
    return {
        "status": "FAIL" if drift > threshold else "PASS",
        "drift_score": drift,
        "threshold": threshold,
        "sample_size": int(len(vecs)),
        "interpretation": "semantic content of text has shifted" if drift > threshold else "stable",
        "backend": backend,
    }


def build_prompt_input_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt_inputs: list[dict[str, Any]] = []

    for record in records:
        record_id = (
            record.get("doc_id")
            or record.get("event_id")
            or record.get("id")
            or record.get("run_id")
            or ""
        )

        source_path = str(
            record.get("source_path")
            or record.get("event_type")
            or record.get("record_type")
            or record.get("name")
            or "unknown"
        )

        preview_parts: list[str] = []

        text_value = record.get("text")
        if isinstance(text_value, str) and text_value.strip():
            preview_parts.append(text_value.strip())

        extracted_facts = record.get("extracted_facts") or []
        if isinstance(extracted_facts, list):
            for fact in extracted_facts:
                if not isinstance(fact, dict):
                    continue
                fact_text = fact.get("text")
                if isinstance(fact_text, str) and fact_text.strip():
                    preview_parts.append(fact_text.strip())

        payload = record.get("payload")
        if isinstance(payload, dict):
            for key in (
                "output_summary",
                "tool_input_summary",
                "tool_output_summary",
                "remediation_description",
                "override_reason",
                "input",
                "output",
            ):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    preview_parts.append(value.strip())

        inputs = record.get("inputs")
        if isinstance(inputs, dict):
            for value in inputs.values():
                if isinstance(value, str) and value.strip():
                    preview_parts.append(value.strip())

        outputs = record.get("outputs")
        if isinstance(outputs, dict):
            for value in outputs.values():
                if isinstance(value, str) and value.strip():
                    preview_parts.append(value.strip())

        content_preview = " ".join(preview_parts).strip()[:8000]

        prompt_inputs.append(
            {
                "doc_id": str(record_id),
                "source_path": source_path,
                "content_preview": content_preview,
            }
        )

    return prompt_inputs


def validate_prompt_inputs(
    records: list[dict[str, Any]],
    quarantine_path: str | Path = "outputs/quarantine/quarantine.jsonl",
) -> dict[str, Any]:
    valid_count = 0
    quarantined: list[dict[str, Any]] = []
    prompt_inputs = build_prompt_input_records(records)

    for item in prompt_inputs:
        try:
            validate(instance=item, schema=PROMPT_INPUT_SCHEMA)
            valid_count += 1
        except ValidationError as exc:
            quarantined.append({"record": item, "error": exc.message})

    qpath = Path(quarantine_path)
    if quarantined:
        ensure_parent(qpath)
        with qpath.open("w", encoding="utf-8") as f:
            for row in quarantined:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {
        "status": "FAIL" if quarantined else "PASS",
        "total_records": len(prompt_inputs),
        "valid": valid_count,
        "quarantined": len(quarantined),
        "quarantine_path": str(qpath),
        "schema": "draft-07",
    }


def validate_verdict_record(record: dict[str, Any]) -> bool:
    record_type = record.get("record_type")

    if record_type == "report_summary":
        required = {"record_type", "overall_score", "verdict"}
        return required.issubset(record.keys())

    if record_type == "criterion_summary":
        required = {"record_type", "criterion", "final_score"}
        return required.issubset(record.keys())

    if record_type == "judicial_opinion":
        required = {"record_type", "criterion", "judge", "judge_score", "argument"}
        return required.issubset(record.keys())

    return False


def check_output_schema_violation_rate(
    verdict_records: list[dict[str, Any]],
    baseline_rate: float | None = None,
    warn_threshold: float = 0.02,
) -> dict[str, Any]:
    total_outputs = len(verdict_records)
    if total_outputs == 0:
        return {
            "status": "WARN",
            "total_outputs": 0,
            "schema_violations": 0,
            "violation_rate": 0.0,
            "trend": "unknown",
            "baseline_violation_rate": baseline_rate,
            "warn_threshold": warn_threshold,
        }

    violations = sum(1 for record in verdict_records if not validate_verdict_record(record))
    violation_rate = violations / total_outputs

    if baseline_rate is None:
        trend = "unknown"
    elif violation_rate < baseline_rate:
        trend = "improving"
    elif violation_rate > baseline_rate:
        trend = "worsening"
    else:
        trend = "stable"

    status = "PASS" if violation_rate == 0 else "WARN"

    return {
        "status": status,
        "total_outputs": total_outputs,
        "schema_violations": violations,
        "violation_rate": round(violation_rate, 4),
        "trend": trend,
        "baseline_violation_rate": baseline_rate,
        "warn_threshold": warn_threshold,
    }


def infer_contract_id(extractions_path: str | None, verdicts_path: str | None, explicit: str | None) -> str:
    if explicit:
        return explicit

    joined = " ".join([p for p in [extractions_path, verdicts_path] if p])
    lowered = joined.lower()

    if "week3" in lowered and "extractions" in lowered:
        return "week3-document-refinery-extractions"
    if "week5" in lowered and "events" in lowered:
        return "week5-event-platform-events"
    if "week2" in lowered and "verdict" in lowered:
        return "week2-judicial-verdict-records"
    if "langsmith" in lowered or "trace" in lowered or "runs" in lowered:
        return "langsmith-trace-records"

    return "unknown-ai-contract"


def append_ai_warn_violation(
    *,
    contract_id: str,
    output_schema_result: dict[str, Any],
    violation_log_path: str | Path | None,
) -> str:
    if violation_log_path:
        path = Path(violation_log_path)
    else:
        path = Path("violation_log") / f"{contract_id}_violations.jsonl"

    entry = {
        "violation_id": str(uuid.uuid4()),
        "contract_id": contract_id,
        "check_id": "llm_output_schema_violation_rate",
        "detected_at": iso_now(),
        "severity": "WARN",
        "metric_name": "output_schema_violation_rate",
        "metric_value": output_schema_result.get("violation_rate"),
        "schema_violations": output_schema_result.get("schema_violations"),
        "total_outputs": output_schema_result.get("total_outputs"),
        "baseline_violation_rate": output_schema_result.get("baseline_violation_rate"),
        "warn_threshold": output_schema_result.get("warn_threshold"),
        "trend": output_schema_result.get("trend"),
        "message": (
            "Structured output schema violation rate exceeded the healthy threshold "
            "and should be monitored as an AI contract warning."
        ),
        "source": "contracts/ai_extensions.py",
    }

    ensure_parent(path)

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return str(path)


def run_all(args: argparse.Namespace) -> dict[str, Any]:
    output: dict[str, Any] = {
        "run_date": datetime.now(UTC).date().isoformat(),
        "mode": args.mode,
    }

    if args.mode in {"all", "embedding", "prompt"}:
        if not args.extractions:
            raise ValueError("--extractions is required for embedding/prompt checks")
        extraction_records = load_jsonl(args.extractions)

        if args.mode in {"all", "embedding"}:
            texts = extract_texts(extraction_records)
            output["embedding_drift"] = check_embedding_drift(
                texts,
                baseline_path=embedding_baseline_path(args.extractions),
                threshold=args.embedding_threshold,
                model=args.embedding_model,
            )

        if args.mode in {"all", "prompt"}:
            output["prompt_input_validation"] = validate_prompt_inputs(extraction_records)

    if args.mode in {"all", "output"}:
        if not args.verdicts:
            raise ValueError("--verdicts is required for structured output checks")
        verdict_records = load_jsonl(args.verdicts)
        output["output_schema"] = check_output_schema_violation_rate(
            verdict_records,
            baseline_rate=args.baseline_rate,
            warn_threshold=args.warn_threshold,
        )

        if output["output_schema"]["status"] == "WARN":
            contract_id = infer_contract_id(args.extractions, args.verdicts, args.contract_id)
            log_path = append_ai_warn_violation(
                contract_id=contract_id,
                output_schema_result=output["output_schema"],
                violation_log_path=args.violation_log,
            )
            output["output_schema"]["violation_log_entry_written"] = log_path

    return output


def main() -> None:
    args = parse_args()
    results = run_all(args)

    output_path = Path(args.output)
    ensure_parent(output_path)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"[OK] mode={args.mode}")
    print(f"[OK] output={output_path}")
    if "embedding_drift" in results:
        print(f"[OK] embedding_status={results['embedding_drift']['status']}")
    if "prompt_input_validation" in results:
        print(f"[OK] prompt_status={results['prompt_input_validation']['status']}")
    if "output_schema" in results:
        print(f"[OK] output_schema_status={results['output_schema']['status']}")
        if results["output_schema"].get("violation_log_entry_written"):
            print(f"[OK] output_schema_violation_log={results['output_schema']['violation_log_entry_written']}")


if __name__ == "__main__":
    main()

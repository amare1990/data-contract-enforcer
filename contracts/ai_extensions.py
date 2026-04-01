#!/usr/bin/env python3
"""AI Contract Extensions for TRP1 Week 8: Data Contract Enforcer.

Implements three AI-specific contract checks:
1. Embedding drift detection
2. Prompt input schema validation
3. Structured LLM output schema violation rate

Example:
    uv run python contracts/ai_extensions.py \
      --mode all \
      --extractions outputs/week3/extractions.jsonl \
      --verdicts outputs/week2/verdicts.jsonl \
      --output validation_reports/ai_extensions.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, UTC
from collections import Counter
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
        "content_preview": {"type": "string", "maxLength": 8000},
    },
    "additionalProperties": False,
}

VERDICT_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["overall_verdict"],
    "properties": {
        "overall_verdict": {"type": "string", "enum": ["PASS", "FAIL", "WARN"]},
        "overall_score": {"type": ["number", "integer"]},
        "confidence": {"type": ["number", "integer"]},
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
        help="Path to Week 3 extractions JSONL",
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
        help="Embedding model name (default: text-embedding-3-small)",
    )
    parser.add_argument(
        "--embedding-threshold",
        type=float,
        default=0.15,
        help="Cosine-distance threshold for embedding drift failure (default: 0.15)",
    )
    parser.add_argument(
        "--warn-threshold",
        type=float,
        default=0.02,
        help="Violation-rate threshold for WARN on structured outputs (default: 0.02)",
    )
    parser.add_argument(
        "--baseline-rate",
        type=float,
        default=None,
        help="Optional baseline violation rate for trend classification",
    )
    return parser.parse_args()


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
    """Fallback embedding when API access/key is unavailable.

    Uses hashed token counts to create a deterministic pseudo-embedding so the
    extension remains runnable in evaluator/local environments.
    """
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

    api_key = None
    try:
        import os

        api_key = os.environ.get("OPENAI_API_KEY")
    except Exception:
        api_key = None

    if OpenAI is not None and api_key:
        client = OpenAI()
        response = client.embeddings.create(input=sample, model=model)
        return np.array([item.embedding for item in response.data], dtype=np.float64)

    return np.array([deterministic_local_embedding(text) for text in sample], dtype=np.float64)


def extract_week3_texts(records: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for record in records:
        if isinstance(record.get("text"), str):
            texts.append(record["text"])
            continue
        for fact in record.get("extracted_facts", []) or []:
            text = fact.get("text")
            if isinstance(text, str):
                texts.append(text)
    return texts


def check_embedding_drift(
    texts: list[str],
    baseline_path: str | Path = "schema_snapshots/embedding_baselines.npz",
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
            "backend": "openai" if OpenAI is not None else "local-hash",
        }

    centroid = vecs.mean(axis=0)
    baseline_path = Path(baseline_path)
    ensure_parent(baseline_path)

    backend = "openai" if OpenAI is not None else "local-hash"

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
        if "text" in record:
            preview = str(record.get("text", ""))[:8000]
        else:
            pieces: list[str] = []
            for fact in record.get("extracted_facts", []) or []:
                text = fact.get("text")
                if isinstance(text, str):
                    pieces.append(text)
            preview = " ".join(pieces)[:8000]

        prompt_inputs.append(
            {
                "doc_id": str(record.get("doc_id", "")),
                "source_path": str(record.get("source_path", "")),
                "content_preview": preview,
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
        with qpath.open("a", encoding="utf-8") as f:
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


def check_output_schema_violation_rate(
    verdict_records: list[dict[str, Any]],
    baseline_rate: float | None = None,
    warn_threshold: float = 0.02,
) -> dict[str, Any]:
    total = len(verdict_records)
    violations = 0

    for record in verdict_records:
        try:
            validate(instance=record, schema=VERDICT_OUTPUT_SCHEMA)
        except ValidationError:
            violations += 1

    rate = violations / max(total, 1)

    if baseline_rate is None:
        trend = "unknown"
    else:
        trend = "rising" if rate > baseline_rate * 1.5 else "stable"

    status = "WARN" if rate > warn_threshold else "PASS"
    if trend == "rising" and status == "PASS":
        status = "WARN"

    return {
        "status": status,
        "total_outputs": total,
        "schema_violations": violations,
        "violation_rate": round(rate, 4),
        "trend": trend,
        "baseline_violation_rate": baseline_rate,
        "warn_threshold": warn_threshold,
    }


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
            texts = extract_week3_texts(extraction_records)
            output["embedding_drift"] = check_embedding_drift(
                texts,
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


if __name__ == "__main__":
    main()

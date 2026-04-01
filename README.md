# Data Contract Enforcer — Usage Guide

This repository implements a contract generation, validation, attribution, and reporting pipeline.

---

# 1. Contract Generation

## Week 3 — Document Refinery

```bash
uv run python contracts/generator.py \
  --source outputs/week3/extractions.jsonl \
  --contract-id week3-document-refinery-extractions \
  --lineage outputs/week4/lineage_snapshots_week3.jsonl \
  --output generated_contracts/
```

---

## Week 5 — Event Platform

```bash
uv run python contracts/generator.py \
  --source outputs/week5/events.jsonl \
  --contract-id week5-event-platform-events \
  --lineage outputs/week4/lineage_snapshots_week5.jsonl \
  --output generated_contracts/
```

---

# 2. Validation Runner

## Week 3 (violated example)

```bash
uv run python contracts/runner.py \
  --contract generated_contracts/week3_extractions.yaml \
  --data outputs/week3/extractions_violated.jsonl \
  --output validation_reports/violated_run.json
```

---

## Week 5

### Baseline

```bash
uv run python contracts/runner.py \
  --contract generated_contracts/week5_event_platform_events.yaml \
  --data outputs/week5/events.jsonl \
  --output validation_reports/week5_baseline.json
```

### Violated

```bash
uv run python contracts/runner.py \
  --contract generated_contracts/week5_event_platform_events.yaml \
  --data outputs/week5/events_violated.jsonl \
  --output validation_reports/week5_violated.json
```

---

# 3. Violation Attribution

```bash
uv run python contracts/attributor.py \
  --violation validation_reports/violated_run.json \
  --lineage outputs/week4/lineage_snapshots.jsonl \
  --contract generated_contracts/week3_extractions.yaml \
  --output violation_log/violations.jsonl
```

---

# 4. Schema Evolution Analysis

```bash
uv run python contracts/schema_analyzer.py \
  --contract-id week3-document-refinery-extractions \
  --output validation_reports/schema_evolution_week3.json
```

---

# 5. AI Contract Extensions

## Embedding drift

```bash
uv run python contracts/ai_extensions.py \
  --mode embedding \
  --extractions outputs/week3/extractions.jsonl \
  --output validation_reports/ai_extensions_embedding.json
```

## Prompt validation

```bash
uv run python contracts/ai_extensions.py \
  --mode prompt \
  --extractions outputs/week3/extractions.jsonl \
  --output validation_reports/ai_extensions_prompt.json
```

## Full AI checks

```bash
uv run python contracts/ai_extensions.py \
  --mode all \
  --extractions outputs/week3/extractions.jsonl \
  --verdicts outputs/week2/verdicts.jsonl \
  --output validation_reports/ai_extensions.json
```

---

# 6. Report Generation

```bash
uv run python contracts/report_generator.py \
  --reports-dir validation_reports \
  --violations violation_log/violations.jsonl \
  --schema-evolution validation_reports/schema_evolution_week3.json \
  --output enforcer_report/report_data.json
```

## With AI metrics

```bash
uv run python contracts/report_generator.py \
  --reports-dir validation_reports \
  --violations violation_log/violations.jsonl \
  --schema-evolution validation_reports/schema_evolution_week3.json \
  --ai-metrics validation_reports/ai_extensions.json \
  --output enforcer_report/report_data.json
```

---

# 7. Inspect Outputs

```bash
cat violation_log/violations.jsonl
```

### Pretty print (JSONL-safe)

```bash
while read -r line; do echo "$line" | python -m json.tool; done < violation_log/violations.jsonl
```

### Check failures

```bash
grep -A 8 -B 2 '"status": "FAIL"' validation_reports/violated_run.json
```

---

# Notes

* Contracts are generated from real data (Week 3 and Week 5 outputs)
* Validation reports are produced from actual runs (not synthetic examples)
* Lineage is used for attribution and blast radius analysis
* AI extensions cover embedding drift and prompt validation


---

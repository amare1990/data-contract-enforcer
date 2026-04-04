# Data Contract Enforcer — Usage Guide

This repository implements contract generation, validation, attribution, schema evolution analysis,
AI contract extensions, and machine-generated enforcer reporting.

## 0. Contract Registry

The updated implementation includes a YAML contract registry at:

```bash
contract_registry/subscriptions.yaml
```

---


## 1. Contract Generation

### Week 3

```bash
uv run python contracts/generator.py \
  --source outputs/week3/extractions.jsonl \
  --contract-id week3-document-refinery-extractions \
  --lineage outputs/week4/lineage_snapshots_week3.jsonl \
  --registry contract_registry/subscriptions.yaml \
  --output generated_contracts/
```

### Week 5

```bash
uv run python contracts/generator.py \
  --source outputs/week5/events.jsonl \
  --contract-id week5-event-platform-events \
  --lineage outputs/week4/lineage_snapshots_week5.jsonl \
  --registry contract_registry/subscriptions.yaml \
  --output generated_contracts/
```

👉 This step also generates statistical baseline artifacts:

```
schema_snapshots/<contract_id>_baselines.json
```

---

## 2. Validation Runner

### Week 3

```bash
# Baseline
uv run python contracts/runner.py \
  --contract generated_contracts/week3_extractions.yaml \
  --data outputs/week3/extractions.jsonl \
  --mode AUDIT \
  --output validation_reports/week3_baseline.json

# Violated
uv run python contracts/runner.py \
  --contract generated_contracts/week3_extractions.yaml \
  --data outputs/week3/extractions_violated.jsonl \
  --mode ENFORCE \
  --output validation_reports/week3_violated.json
```

---

### Week 5

```bash
# Baseline
uv run python contracts/runner.py \
  --contract generated_contracts/week5_event_platform_events.yaml \
  --data outputs/week5/events_canonical.jsonl \
  --mode AUDIT \
  --output validation_reports/week5_baseline.json

# Violated
uv run python contracts/runner.py \
  --contract generated_contracts/week5_event_platform_events.yaml \
  --data outputs/week5/events_violated.jsonl \
  --mode ENFORCE \
  --output validation_reports/week5_violated.json
```

---

## 3. Violation Attribution (REQUIRED)

This step converts validation failures into **blame chains + blast radius**.

### Week 3

```bash
uv run python contracts/attributor.py \
  --violation validation_reports/week3_violated.json \
  --lineage outputs/week4/lineage_snapshots_week3.jsonl \
  --contract generated_contracts/week3_extractions.yaml \
  --registry contract_registry/subscriptions.yaml \
  --output violation_log/week3-document-refinery-extractions_violations.jsonl
```

### Week 5

```bash
uv run python contracts/attributor.py \
  --violation validation_reports/week5_violated.json \
  --lineage outputs/week4/lineage_snapshots_week5.jsonl \
  --contract generated_contracts/week5_event_platform_events.yaml \
  --registry contract_registry/subscriptions.yaml \
  --output violation_log/week5-event-platform-events_violations.jsonl
```

---

## 4. Schema Evolution Analysis

### Week 3

```bash
uv run python contracts/schema_analyzer.py \
  --contract-id week3-document-refinery-extractions \
  --output validation_reports/schema_evolution_week3.json
```

### Week 5

```bash
uv run python contracts/schema_analyzer.py \
  --contract-id week5-event-platform-events \
  --output validation_reports/schema_evolution_week5.json
```

---

## 5. AI Contract Extensions

### Week 3

```bash
uv run python contracts/ai_extensions.py \
  --mode all \
  --extractions outputs/week3/extractions.jsonl \
  --verdicts outputs/week2/verdicts_canonical.jsonl \
  --contract-id week3-document-refinery-extractions \
  --output validation_reports/week3_ai_extensions.json
```

### Week 5

```bash
uv run python contracts/ai_extensions.py \
  --mode all \
  --extractions outputs/week5/events.jsonl \
  --verdicts outputs/week2/verdicts_canonical.jsonl \
  --contract-id week5-event-platform-events \
  --output validation_reports/week5_ai_extensions.json
```

👉 This step may append WARN entries to:

```
violation_log/<contract_id>_violations.jsonl
```

---

## 6. Report Generation

### Week 3

```bash
uv run python contracts/report_generator.py \
  --baseline-report validation_reports/week3_baseline.json \
  --violated-report validation_reports/week3_violated.json \
  --contract-id week3-document-refinery-extractions \
  --violations violation_log/week3-document-refinery-extractions_violations.jsonl \
  --schema-evolution validation_reports/schema_evolution_week3.json \
  --ai-metrics validation_reports/week3_ai_extensions.json \
  --output enforcer_report/week3_report_data.json
```

### Week 5

```bash
uv run python contracts/report_generator.py \
  --baseline-report validation_reports/week5_baseline.json \
  --violated-report validation_reports/week5_violated.json \
  --contract-id week5-event-platform-events \
  --violations violation_log/week5-event-platform-events_violations.jsonl \
  --schema-evolution validation_reports/schema_evolution_week5.json \
  --ai-metrics validation_reports/week5_ai_extensions.json \
  --output enforcer_report/week5_report_data.json
```

---

## 🔍 Artifact Roles (Important)

| Artifact                                     | Purpose                           |
| -------------------------------------------- | --------------------------------- |
| `generated_contracts/*.yaml`                 | Contract definitions              |
| `schema_snapshots/*_baselines.json`          | Statistical baselines (generator) |
| `validation_reports/*_baseline.json`         | Clean validation run              |
| `validation_reports/*_violated.json`         | Failure run                       |
| `violation_log/*.jsonl`                      | Attribution + AI warnings         |
| `validation_reports/schema_evolution_*.json` | Schema diff + migration           |
| `validation_reports/*_ai_extensions.json`    | AI metrics                        |
| `enforcer_report/*.json`                     | Final machine-generated report    |


---

## 7. Inspect Outputs

```bash
cat violation_log/week3_violations.jsonl
```

```bash
while read -r line; do echo "$line" | python -m json.tool; done < violation_log/week5_violations.jsonl
```

```bash
grep -A 8 -B 2 '"status": "FAIL"' validation_reports/week5_violated.json
```

---

## Notes

- registry subscriptions are maintained manually in `contract_registry/subscriptions.yaml`
- generator can inject registry-aware downstream consumers into generated contracts
- attributor queries the registry first, then enriches with lineage depth
- runner now records `enforcement_mode`, `blocking_violation_count`, and `would_block_pipeline`

# Run generator

```bash
uv run python contracts/generator.py \
  --source outputs/week3/extractions.jsonl \
  --contract-id week3-document-refinery-extractions \
  --lineage outputs/week4/lineage_snapshots.jsonl \
  --output generated_contracts/
```

# To run the validation runner-> runner.py

```bash

uv run python contracts/runner.py \
>   --contract generated_contracts/week3_extractions.yaml \
>   --data outputs/week3/extractions_violated.jsonl \
>   --output validation_reports/violated_run.json

```

# To run the attributer -> attributer.py

```bash
uv run python contracts/attributor.py \
  --violation validation_reports/violated_run.json \
  --lineage outputs/week4/lineage_snapshots.jsonl \
  --contract generated_contracts/week3_extractions.yaml \
  --output violation_log/violations.jsonl
```

### Then inspect the result

```bash

cat violation_log/violations.jsonl

# If you want a cleaner look:

python -m json.tool < violation_log/violations.jsonl

# That last command may complain because JSONL is multiple JSON objects, so the safer version is:

while read -r line; do echo "$line" | python -m json.tool; done < violation_log/violations.jsonl

# Useful sanity check

grep -A 8 -B 2 '"status": "FAIL"' validation_reports/violated_run.json

```

# To run contracts/schema_analyzer.py

```bash

uv run python contracts/schema_analyzer.py \
  --contract-id week3-document-refinery-extractions \
  --output validation_reports/schema_evolution_week3.json

```

# To run contracts/report_generator.py

```bash

uv run python contracts/report_generator.py \
  --reports-dir validation_reports \
  --violations violation_log/violations.jsonl \
  --schema-evolution validation_reports/schema_evolution_week3.json \
  --output enforcer_report/report_data.json

  # If you later add AI metrics:

  uv run python contracts/report_generator.py \
  --reports-dir validation_reports \
  --violations violation_log/violations.jsonl \
  --schema-evolution validation_reports/schema_evolution_week3.json \
  --ai-metrics validation_reports/ai_extensions.json \
  --output enforcer_report/report_data.json

```

# To run contracts/ai_extensions.py

```bash

#Mode = embedding

uv run python contracts/ai_extensions.py \
  --mode embedding \
  --extractions outputs/week3/extractions.jsonl \
  --output validation_reports/ai_extensions_embedding.json

# Mode = prompt
  uv run python contracts/ai_extensions.py \
  --mode prompt \
  --extractions outputs/week3/extractions.jsonl \
  --output validation_reports/ai_extensions_prompt.json

# Mode = all

uv run python contracts/ai_extensions.py \
  --mode all \
  --extractions outputs/week3/extractions.jsonl \
  --verdicts outputs/week2/verdicts.jsonl \
  --output validation_reports/ai_extensions.json

```


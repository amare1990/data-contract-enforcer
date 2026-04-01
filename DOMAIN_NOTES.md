# DOMAIN_NOTES.md

## Focus and Source Priority

The Week 7 project brief is treated as the canonical specification. The practitioner manual is used only as an auxiliary implementation guide. Where discrepancies arise, the main brief governs schema definitions, contract semantics, and evaluation expectations.

---

# 0. Dataset grounding

## Week 3 — Document Refinery

* Source: `outputs/week3/extractions.jsonl`
* Contract: `generated_contracts/week3_extractions.yaml`

Schema characteristics:

* semi-structured tabular data
* weak typing (`value`)
* numeric confidence field

---

## Week 5 — Event Platform

* Source: `outputs/week5/events.jsonl`
* Contract: `generated_contracts/week5_event_platform_events.yaml`

Schema characteristics:

* event envelope + polymorphic payload
* nested JSON
* sequence guarantees

---

# 1. Backward-compatible vs breaking schema changes

## Definition

### Backward-compatible

Consumers continue working without modification.

### Breaking

Existing consumers fail or misinterpret data.

---

## Examples from my systems

### Backward-compatible (3 examples)

1. Week 3:

```text
Add optional field: review_notes
```

2. Week 5:

```text
Add metadata field: "source_service"
```

3. Week 5:

```text
Add new event_type enum value
```

---

### Breaking (3 examples)

1. Week 3:

```text
confidence: float → integer scale change (semantic break)
```

2. Week 5:

```text
sequence_number: int → string
```

3. Week 5:

```text
payload field type change (numeric → string)
```

---

## Key insight

> Breaking changes are often **semantic**, not structural.

---

# 2. Confidence scale failure (end-to-end)

## Change

```text
confidence: float [0.0–1.0] → integer [0–100]
```

---

## Failure propagation

### Step 1 — Week 3 output

New data:

```json
{"confidence": 87}
```

---

### Step 2 — Week 4 Cartographer

Cartographer:

* infers schema from observed data
* does NOT detect semantic change

Result:

```text
confidence still treated as valid numeric field
```

---

### Step 3 — Downstream failure

* models assume normalized probability
* statistical drift spikes
* incorrect ranking / filtering

---

## Contract clause to prevent this

```yaml
version: 1
dataset: week3_extractions

fields:
  - name: confidence
    type: float
    constraints:
      min: 0.0
      max: 1.0

statistical_checks:
  - field: confidence
    distribution: beta
    expected_range: [0.0, 1.0]
```

---

## Insight

> Structural validation passes; semantic validation fails.

---

# 3. Lineage → blame chain (step-by-step)

## Inputs

* lineage graph (Week 4)
* validation failure (Week 7)

---

## Step-by-step process

### Step 1 — Detect violation

Example:

```text
confidence out of range
```

---

### Step 2 — Identify dataset node

```text
node = "week3_extractions"
```

---

### Step 3 — Traverse upstream

Graph traversal:

```text
DFS/BFS over incoming edges
```

Pseudo:

```python
ancestors = nx.ancestors(lineage_graph, node)
```

---

### Step 4 — Identify transformations

Look for nodes where:

```text
type = TRANSFORMATION
```

---

### Step 5 — Map to source files

Using metadata:

```text
source_file
```

---

### Step 6 — Construct blame chain

```text
Dataset → Transformation → Source file → (optional commit)
```

---

## Example

```text
week3_extractions
  ← transformation: extract_confidence
    ← file: extractor.py
```

---

## Insight

> Attribution = graph traversal + metadata resolution

---

# 4. Data contract for LangSmith trace_record

## Bitol-compatible YAML

```yaml
version: 1
dataset: trace_record

fields:
  - name: trace_id
    type: string
    required: true

  - name: latency_ms
    type: float
    required: true

  - name: input_tokens
    type: int

  - name: output_tokens
    type: int

  - name: prompt
    type: string

  - name: response
    type: string

constraints:
  - field: latency_ms
    min: 0

statistical_checks:
  - field: latency_ms
    mean_range: [0, 5000]

ai_checks:
  - field: response
    constraint: "must_be_json"

  - field: prompt
    constraint: "no_pii"
```

---

## Coverage

* structural → types + required
* statistical → latency distribution
* AI-specific → JSON output + safety

---

# 5. Why contract systems fail in production

## Most common failure mode

```text
Contracts become stale
```

---

## Why this happens

1. schema inferred from bad data
2. no regeneration
3. no ownership
4. no statistical validation

---

## Evidence from my system

Week 5:

* contract inferred incorrectly for polymorphic payload
* 178/200 checks errored

---

## How my architecture prevents this

1. regeneration from real data
2. schema snapshots
3. statistical checks
4. lineage-based attribution
5. AI validation layer

---

## Key insight

> Contracts fail when treated as documentation instead of executable artifacts.

---

# 6. Contract Quality Evaluation

## Week 3

* usable contract
* accuracy ≥ 70%

---

## Week 5

### Baseline

* total: 200
* passed: 21
* errored: 178

### Violated

* total: 200
* failed: 2

---

## Root cause

> Polymorphic payload + nested JSON breaks unified schema inference

---

## Final insight

```text
Tabular systems → contract inference works
Event systems → requires schema-per-event-type
```

---

# 7. Final conclusion

This system demonstrates:

* contracts must be generated, validated, and versioned
* semantic validation is critical
* lineage enables attribution
* generic inference fails on event-sourced systems

---

# 8. Artifact mapping

* contracts → `generated_contracts/`
* validation → `validation_reports/`
* lineage → `outputs/week4/`
* snapshots → `schema_snapshots/`

---

# FINAL TAKEAWAY

> Data contracts are not schemas — they are executable guarantees over evolving systems.

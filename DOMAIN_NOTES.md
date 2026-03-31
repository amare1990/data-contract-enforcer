# DOMAIN_NOTES.md

## Focus and Source Priority

This project document is the primary specification for the week, and the practitioner manual is treated as an implementation aid rather than a replacement source of truth. Where the manual suggests workflow, commands, staging, or debugging tactics, those are used only insofar as they support the mandatory requirements, schemas, deliverables, and rubric in the main project brief. fileciteturn0file0 fileciteturn0file1

## 1. Backward-compatible vs. breaking schema changes

A backward-compatible schema change preserves existing downstream consumers that rely on the prior contract. Existing readers can keep operating without code changes because the original fields, types, constraints, and meanings are still valid. In the Week 8 taxonomy, this includes additive or widening changes such as adding a nullable field or adding an enum value that old consumers can safely ignore. By contrast, a breaking change alters meaning, structure, cardinality, or typing in a way that invalidates existing assumptions in downstream logic, which is exactly the class of failure this week is designed to prevent. fileciteturn0file0

### Three backward-compatible examples from Weeks 1–5

**Example A — Week 3 extraction_record: add a nullable top-level field**

If `extraction_record` gains a nullable field such as `review_notes: string | null`, existing consumers that only read `doc_id`, `extracted_facts`, `entities`, and `extracted_at` continue to function. This matches the project taxonomy: an added nullable column is compatible because downstream consumers may ignore it. fileciteturn0file0

**Example B — Week 4 lineage_snapshot: add an enum value to `nodes[].type`**

The canonical schema already lists `FILE|TABLE|SERVICE|MODEL|PIPELINE|EXTERNAL` for `nodes[].type`. If my earlier implementation omitted `EXTERNAL` and the schema later added it, that is typically additive rather than destructive, provided downstream consumers are written to tolerate unfamiliar enum members. The project brief explicitly classifies additive enum changes as usually compatible. fileciteturn0file0

**Example C — Week 5 event_record: widening a numeric representation without changing semantics**

A widening type change such as storing `sequence_number` in a larger integer representation, while preserving monotonicity and exact value semantics, is usually compatible. The project taxonomy names widening changes like `INT -> BIGINT` as generally safe, subject to validation that existing values are preserved. The same principle applies to the event stream so long as sequence ordering and gap detection still behave identically. fileciteturn0file0

### Three breaking examples from Weeks 1–5

**Example D — Week 3 extraction_record: `extracted_facts[].confidence` changes from 0.0–1.0 float to 0–100 integer**

This is the flagship failure in the brief. It is breaking even if the field name remains the same because the semantics of the value distribution change, downstream logic silently misreads the scale, and statistical checks are required to catch it if naive type checks do not. The project text explicitly frames this as a production incident pattern. fileciteturn0file0

**Example E — Week 2 verdict_record: rename `overall_verdict` to `final_verdict`**

A consumer expecting `overall_verdict` with enum `{PASS, FAIL, WARN}` will either error or silently default when the field disappears. The schema evolution taxonomy explicitly classifies column renames as breaking, requiring aliasing and a deprecation period. fileciteturn0file0

**Example F — Week 4 lineage_snapshot: remove `edges[].relationship` or change its legal domain**

The Week 4 schema requires each edge to carry one of `IMPORTS|CALLS|READS|WRITES|PRODUCES|CONSUMES`. Removing that field, or replacing these values with a new incompatible vocabulary, breaks graph traversal logic used later by the ViolationAttributor to infer upstream blame chains and blast radius. The project taxonomy treats removals as breaking and enum removals as breaking. fileciteturn0file0

## 2. Confidence scale failure from Week 3 into Week 4, and the contract clause that catches it

The canonical Week 3 `extraction_record` defines `extracted_facts[].confidence` as a float in the closed interval `0.0–1.0`, not `0–100`. The project brief explicitly warns that changing the scale to percentage form can allow the pipeline to keep running while producing wrong downstream output. That is the exact silent corruption mode this project exists to stop. fileciteturn0file0

### Failure trace into the Cartographer

**Step 1 — Week 3 changes the meaning of confidence without changing the interface name.**

Suppose the extractor continues to emit `extracted_facts[].confidence`, but writes `87.0` instead of `0.87`. Structurally, the field still looks numeric. A weak consumer or weak validator may therefore accept the record. fileciteturn0file0

**Step 2 — Week 4 ingests extraction output as lineage metadata.**

The dependency map states that `Week 3 extraction_record -> Week 4 lineage`, where `doc_id becomes a node, facts become metadata`. That means the Cartographer is a downstream consumer of the extraction payload and may carry confidence-derived metadata into the lineage graph or related annotations. fileciteturn0file0

**Step 3 — Confidence-dependent heuristics become distorted.**

Any downstream logic that thresholds on confidence, ranks facts by confidence, computes averages, or stores confidence summaries in node metadata is now off by two orders of magnitude. A threshold intended to treat `>= 0.80` as high-confidence now admits virtually every scaled value. Mean or percentile summaries become numerically meaningless relative to the prior baseline. This is why the brief distinguishes structural violations from statistical ones. fileciteturn0file0

**Step 4 — The lineage graph becomes a propagation surface for corrupted trust metadata.**

Once bad confidence values are attached to graph nodes or edges, downstream components using the Week 4 lineage graph—including the Week 8 ViolationAttributor—inherit tainted metadata. Even if traversal still works, the interpretive layer becomes less trustworthy because the underlying evidence weights were corrupted upstream. fileciteturn0file0

**Step 5 — The Enforcer should block this before propagation.**

The correct control is not merely “field exists and is numeric.” The contract must assert range semantics and should also baseline the numeric distribution so the 0.0–1.0 to 0–100 shift is caught even when type checks pass. The main brief explicitly requires both a hard range clause and a statistical drift rule using baseline mean and standard deviation. fileciteturn0file0

### Bitol-style YAML clause to catch the scale change

```yaml
kind: DataContract
apiVersion: v3.0.0
id: week3-document-refinery-extractions
info:
  title: Week 3 Document Refinery - Extraction Records
  version: 1.0.0
  owner: week3-team
schema:
  extracted_facts:
    type: array
    items:
      confidence:
        type: number
        required: true
        minimum: 0.0
        maximum: 1.0
        description: Confidence score for extracted fact. Breaking change if scale changes from 0.0-1.0 to 0-100.
quality:
  type: SodaChecks
  specification:
    checks for extractions:
      - min(confidence_mean) >= 0.0
      - max(confidence_mean) <= 1.0
      - max(confidence) <= 1.0
      - min(confidence) >= 0.0
```

This clause encodes the semantic promise that the field is a normalized probability-like score rather than a percentage integer. It mirrors the project’s own Week 3 example and quality checks. fileciteturn0file0

## 3. How the Enforcer uses the Week 4 lineage graph to produce a blame chain

The lineage graph is the architectural hinge of the week. The project brief requires the ContractGenerator to inject lineage context, the ValidationRunner to emit failing checks, and the ViolationAttributor to combine failures, lineage, and git history into a ranked blame chain. The practitioner manual reinforces the same pipeline order: first detect a concrete failure, then traverse lineage, then run git history and blame, then write a structured violation log entry. fileciteturn0file0 fileciteturn0file1

### Step-by-step blame-chain construction

**Step 1 — ValidationRunner identifies the failing schema element.**

A validation report contains a `check_id`, `column_name`, `status`, `actual_value`, `expected`, severity, and failing record count. For the canonical example, the failing element is `extracted_facts[*].confidence` in Week 3, with a range failure indicating percentage-scale values. fileciteturn0file0

**Step 2 — Map the failing field to the producing system node.**

The attributor starts from the failing schema element and maps it to the corresponding producer in the lineage graph. In this case, the producer is a Week 3 extraction-producing node or file. The main brief says to start from the failing schema element, find the upstream node that produces it, and stop at the first external boundary or file-system root. The manual’s implementation sketch similarly searches FILE nodes associated with the failing system and returns candidate paths. fileciteturn0file0 fileciteturn0file1

**Step 3 — Traverse upstream by lineage edges.**

The required traversal is breadth-first search over the Week 4 graph. Conceptually, the traversal walks from the failing output field to the file, service, or pipeline nodes that produced or transformed it using `READS`, `WRITES`, `PRODUCES`, `CONSUMES`, `CALLS`, and `IMPORTS` relationships. Breadth-first search is appropriate because the project wants the nearest plausible origin first, not an arbitrary deep ancestor. fileciteturn0file0

**Step 4 — Build a candidate set of upstream source files.**

Once upstream traversal reaches file-producing nodes, the attributor extracts file paths from node metadata. Those files become git investigation targets. In practical terms, this usually yields files like the Week 3 extractor implementation, transformation helpers, or schema-normalization code. The manual explicitly proposes pulling `node['metadata']['path']` from matching FILE nodes. fileciteturn0file1

**Step 5 — Interrogate git history for recent causal candidates.**

For each candidate file, the required commands are `git log --follow --since="14 days ago" --format=...` and then targeted `git blame` on relevant line ranges. This moves the attributor from structural provenance to human and temporal provenance: who changed the code, when, and with what commit message. fileciteturn0file0

**Step 6 — Score and rank candidates.**

The brief defines the confidence formula as `base = 1.0 − (days_since_commit × 0.1)`, reduced by `0.2` for each lineage hop between the blamed file and the failing column. This explicitly combines recency and topological distance. The manual’s scoring sketch applies the same two factors and sorts descending by confidence. fileciteturn0file0 fileciteturn0file1

**Step 7 — Compute blast radius from lineage downstreams.**

The same lineage context that enables upstream blame also enables downstream impact analysis. The contract stores `downstream_consumers[]`, and the violation log includes `affected_nodes`, `affected_pipelines`, and estimated affected record count. For the Week 3 confidence example, the blast radius includes the Week 4 Cartographer and any dependent Week 8 analyses that ingest the lineage graph. fileciteturn0file0 fileciteturn0file1

**Step 8 — Write the violation record.**

The final deliverable is a structured JSONL violation entry containing the failing check, detection timestamp, ranked blame chain, and blast radius. That record then becomes an input to the later report and alerting steps. fileciteturn0file0

## 4. Bitol-compatible data contract for LangSmith trace_record

The project document defines a canonical `trace_record` with fields for identity, run type, timing, token counts, cost, and session lineage, and it explicitly requires one structural clause, one statistical clause, and one AI-specific clause. The contract below is therefore built directly from that target schema and Week 8’s AI extension objectives. fileciteturn0file0

```yaml
kind: DataContract
apiVersion: v3.0.0
id: langsmith-trace-records
info:
  title: LangSmith Trace Export Records
  version: 1.0.0
  owner: platform-observability
  description: >
    One record per LangSmith run exported from the monitored agent systems.
    Used for timing, token, cost, and AI-run quality validation.
servers:
  local:
    type: local
    path: outputs/traces/runs.jsonl
    format: jsonl
terms:
  usage: Internal monitoring and AI contract enforcement only.
  limitations: Trace exports must preserve token accounting and valid run typing.
schema:
  id:
    type: string
    format: uuid
    required: true
    unique: true
    description: Primary run identifier.
  name:
    type: string
    required: true
    description: Chain, tool, retriever, embedding, or LLM run name.
  run_type:
    type: string
    required: true
    enum: [llm, chain, tool, retriever, embedding]
    description: Must be one of the five canonical LangSmith run types.
  inputs:
    type: object
    required: true
    description: Structured input payload for the run.
  outputs:
    type: object
    required: false
    description: Structured output payload for the run.
  error:
    type: string
    required: false
    description: Null or an error string if the run failed.
  start_time:
    type: string
    format: date-time
    required: true
    description: Run start timestamp in ISO 8601.
  end_time:
    type: string
    format: date-time
    required: true
    description: Run end timestamp in ISO 8601 and must be greater than start_time.
  total_tokens:
    type: integer
    minimum: 0
    required: true
    description: Total token count for the run.
  prompt_tokens:
    type: integer
    minimum: 0
    required: true
    description: Prompt token count.
  completion_tokens:
    type: integer
    minimum: 0
    required: true
    description: Completion token count.
  total_cost:
    type: number
    minimum: 0.0
    required: true
    description: Total run cost in USD.
  parent_run_id:
    type: string
    required: false
    description: Nullable parent run UUID.
  session_id:
    type: string
    format: uuid
    required: true
    description: Session identifier joining runs into a conversation or job.
quality:
  type: SodaChecks
  specification:
    checks for traces:
      - missing_count(id) = 0
      - missing_count(run_type) = 0
      - missing_count(start_time) = 0
      - missing_count(end_time) = 0
      - min(total_cost) >= 0
      - min(total_tokens) >= 0
      - min(prompt_tokens) >= 0
      - min(completion_tokens) >= 0
      - invalid_count(run_type) = 0
      - end_time > start_time
      - total_tokens = prompt_tokens + completion_tokens
      - avg(total_cost) < 1.0
      - p95(total_tokens) < 50000
ai_extensions:
  structured_output_enforcement:
    description: Validate that outputs for structured LLM runs conform to the expected JSON schema for the prompt version.
    failure_mode: llm_output_schema
  trace_integrity:
    description: Run-level token accounting and timing integrity must hold for every trace record.
  anomaly_detection:
    description: Alert when schema violation rate or token-cost profile trends upward relative to baseline.
```

### Why this satisfies the prompt

The **structural clauses** are the required fields, UUID/date-time formats, enum constraint on `run_type`, and the timing and token arithmetic invariants. The **statistical clauses** are distribution-oriented checks such as `avg(total_cost) < 1.0` and `p95(total_tokens) < 50000`, which represent bounded operational norms rather than mere structure. The **AI-specific clause** is the structured output enforcement and anomaly detection for LLM-oriented traces, aligning with the project’s emphasis on AI contract extensions beyond conventional tabular contracts. fileciteturn0file0

## 5. The most common production failure mode of contract enforcement systems, why contracts get stale, and how this architecture prevents it

The most common production failure mode is not total absence of validation; it is stale validation that checks yesterday’s assumptions while today’s systems evolve underneath it. In practice, contracts degrade when schemas change informally, downstream consumers adapt locally, and the contract layer is not regenerated, diffed, enforced, or socially treated as a real interface boundary. The result is a false sense of safety: checks exist, but they do not constrain the live system. The main brief says this week’s real problem is that prior systems were already talking to one another “without contracts,” and the manual explicitly warns against silently redefining the contract to fit broken data. fileciteturn0file0 fileciteturn0file1

### Why contracts get stale

**Reason 1 — Producers change code faster than interface documents are updated.**

A developer modifies a field name, value range, enum, or nested object shape to satisfy a local feature. If the contract is hand-maintained and decoupled from the generator or validator pipeline, it lags immediately. fileciteturn0file0

**Reason 2 — Teams optimize for passing pipelines, not preserving meaning.**

A field may remain syntactically valid while its semantics drift. The confidence-scale example is the canonical case: the column is still numeric, but its meaning changed catastrophically. Without statistical baselines, stale contracts miss the break. fileciteturn0file0

**Reason 3 — Lineage and ownership are missing.**

When a violation appears, organizations often cannot determine which system owns the break, who changed it, or who else is impacted. That uncertainty encourages ad hoc local patches instead of contract repair. The project’s emphasis on blame chains and blast radius is a direct answer to this problem. fileciteturn0file0

### How this Week 8 architecture resists staleness

**First, contracts are generated from real outputs.**

The ContractGenerator profiles live JSONL outputs rather than relying purely on handwritten documentation. That makes the contract refreshable and closer to observed truth. The quality bar is not zero manual work, but a generated contract that is mostly trustworthy on first pass. fileciteturn0file0

**Second, snapshots are written every run and diffed over time.**

The SchemaEvolutionAnalyzer depends on timestamped schema snapshots, making change detection temporal rather than anecdotal. This directly addresses staleness because the system can identify not just what the contract is now, but how it changed and when. fileciteturn0file0turn0file1

**Third, validation includes both structural and statistical checks.**

Static structure alone is insufficient. The baseline mean/stddev mechanism is specifically designed to catch silent corruption where a type still passes but the value distribution no longer matches prior reality. fileciteturn0file0turn0file1

**Fourth, violations are attributed, not merely logged.**

Because the ViolationAttributor ties failures to lineage and git history, a stale or broken contract becomes actionable. The system names the likely file, commit, author, and downstream blast radius. That shrinks the time between detection and repair. fileciteturn0file0turn0file1

**Fifth, AI-specific contracts extend the protection surface.**

The architecture explicitly covers embedding drift, prompt input validation, and structured LLM output enforcement. This matters because modern pipelines can remain formally tabular while still failing at the AI boundary. A contract system that ignores those boundaries becomes stale relative to the real platform. fileciteturn0file0

### My conclusion

The architecture prevents staleness by treating contracts as executable artifacts with regeneration, validation, temporal diffing, attribution, and reporting—not as static prose. That is the correct design choice for this week because the stated client question is not merely “what is the schema?” but “can you make sure this never breaks silently again?” fileciteturn0file0

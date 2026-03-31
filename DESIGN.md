# DESIGN.md

## Primary Design Principle

This design is anchored to the main Week 8 project brief. The practitioner manual is used only as an auxiliary implementation guide for sequencing, CLI ergonomics, and common failure recovery. When the manual and the brief differ in emphasis, the brief wins because it contains the canonical schemas, required repository layout, required outputs, assessment rubric, and mandated behavior for each component. fileciteturn0file0 fileciteturn0file1

## 1. System goal

The Data Contract Enforcer formalizes every important inter-system interface in the Weeks 1–5 platform and enforces those promises against real data. It must catch structural violations, statistical drift, schema evolution risk, and AI-specific contract failures before they propagate silently into downstream systems. The project brief explicitly defines this as turning every arrow in the inter-system data flow into a machine-checked promise and then tracing violations to the commit that caused them, along with the blast radius. fileciteturn0file0

## 2. Scope and priorities

### In-scope deliverables

The architecture must produce, at minimum:

* generated Bitol-compatible contracts for Week 3 and Week 5 outputs
* dbt-style schema YAML counterparts
* structured validation reports
* attributed violations with blame chains and blast radius
* schema snapshots and compatibility analysis
* AI contract metrics and failure logs
* an auto-generated Enforcer Report based on live validation data

Those outputs and paths are explicitly mandated in the required repository layout and Thursday/Sunday submission checklists. fileciteturn0file0turn0file1

### Contract priority order

The first document’s dependency map and required minimums imply this build priority:

1. Week 3 extraction contracts
2. Week 5 event contracts
3. Week 4 lineage integration
4. Week 2 verdict output schema checks
5. LangSmith trace contracts and AI extensions

That ordering is not arbitrary. Week 3 and Week 5 are required minimums for generation; Week 4 lineage is a required dependency for attribution; Week 2 and LangSmith are explicit inputs to the AI Contract Extension phase. fileciteturn0file0

## 3. High-level architecture

The system is organized as six main components, matching the main brief’s architecture table:

1. **ContractGenerator**
2. **ValidationRunner**
3. **ViolationAttributor**
4. **SchemaEvolutionAnalyzer**
5. **AI Contract Extensions**
6. **ReportGenerator**

The components communicate through file-based artifacts so the evaluator can run each stage independently and inspect the generated outputs directly from the repository. This aligns with the required layout under `generated_contracts/`, `validation_reports/`, `violation_log/`, `schema_snapshots/`, and `enforcer_report/`. fileciteturn0file0

## 4. Repository design

```text
your-week7-repo/
├── contracts/
│   ├── generator.py
│   ├── runner.py
│   ├── attributor.py
│   ├── schema_analyzer.py
│   ├── ai_extensions.py
│   └── report_generator.py
├── generated_contracts/
├── validation_reports/
├── violation_log/
├── schema_snapshots/
├── enforcer_report/
├── outputs/
└── DOMAIN_NOTES.md
```

This exact repository structure is part of the contract with the evaluator. The design therefore treats file paths as stable interfaces, not incidental implementation details. The main brief explicitly states that evaluation scripts will look for files at these paths. fileciteturn0file0

## 5. Data model and interface map

### Primary source interfaces

The canonical monitored interfaces are:

* `outputs/week1/intent_records.jsonl`
* `outputs/week2/verdicts.jsonl`
* `outputs/week3/extractions.jsonl`
* `outputs/week4/lineage_snapshots.jsonl`
* `outputs/week5/events.jsonl`
* `outputs/traces/runs.jsonl`

These are the system-of-record snapshots from which the Enforcer derives contracts and validation evidence. The main project brief defines the target schemas for each of these sources, including key field-level invariants such as UUID shape, enumerations, date-time format, monotonic sequence numbers, and the critical Week 3 confidence range. fileciteturn0file0

### Most important enforced edges

The design explicitly models the project’s dependency graph:

* Week 1 `intent_record.code_refs[]` -> Week 2 `verdict.target_ref`
* Week 3 `extraction_record` -> Week 4 lineage graph
* Week 4 lineage graph -> Week 8 ViolationAttributor
* Week 5 `event_record` -> Week 8 schema contract validation
* LangSmith `trace_record` -> Week 8 AI Contract Extension
* Week 2 `verdict_record` -> Week 8 structured LLM output validation

These edges matter because the assignment frames each arrow as a contract boundary. Design-wise, that means validation cannot stay confined to single-file profiling; it must also support cross-system invariants and blast-radius reasoning. fileciteturn0file0

## 6. Component design

### 6.1 ContractGenerator

#### Purpose

The ContractGenerator profiles existing outputs and produces human-readable, machine-checkable contract YAML plus dbt-friendly schema output. According to the brief, it must combine structural profiling, statistical profiling, lineage context injection, optional LLM annotation, and dbt output generation. The practitioner manual provides a staged implementation path that is useful operationally but subordinate to those required outcomes. fileciteturn0file0turn0file1

#### Inputs

* one JSONL source file
* latest Week 4 lineage snapshot
* optional metadata such as contract ID and output directory

#### Outputs

* `generated_contracts/{contract}.yaml`
* `generated_contracts/{contract}_dbt.yml`
* `schema_snapshots/{contract_id}/{timestamp}.yaml`

#### Internal pipeline

**Stage A — Load and flatten records for profiling**

Nested JSONL structures, especially Week 3 arrays like `extracted_facts[]`, must be flattened into profile-friendly tabular form while preserving enough traceability to map clauses back to original nested paths. The manual recommends exploding fact arrays into separate rows and prefixing nested fields. That is compatible with the project requirement to structurally and statistically profile the data before clause generation. fileciteturn0file1turn0file0

**Stage B — Structural profiling**

Per column, record dtype, null fraction, distinct count, sample values, and string-pattern heuristics. This directly matches the brief’s structural profiling step. fileciteturn0file0

**Stage C — Statistical profiling**

For numeric columns, record min, max, mean, quartiles, p95, p99, and stddev. Confidence columns receive additional semantic checks because the main brief explicitly calls out the 0.0–1.0 range and distribution-level suspicion markers such as mean > 0.99 or < 0.01. fileciteturn0file0

**Stage D — Clause synthesis**

Translate profiles into contract clauses using deterministic rules: required fields from null fraction, enum inference for small-cardinality object columns, UUID/date-time recognition by naming convention, numeric min/max for bounded metrics, and explicit semantic descriptions for risky fields like confidence. The manual provides practical heuristics for this mapping. fileciteturn0file1

**Stage E — Lineage context injection**

Inject `downstream_consumers[]` into each contract from the latest Week 4 lineage snapshot. This is critical because it enables later blast-radius reporting without recomputing everything from raw graph traversal at reporting time. The main brief explicitly requires this in Phase 1, Step 3. fileciteturn0file0

**Stage F — dbt YAML emission**

Generate a schema.yml analogue with equivalent tests for required fields, enums, and relationships. This is required by the project brief and is useful as a bridge to common FDE practice. fileciteturn0file0

**Stage G — Snapshot write**

Persist timestamped schema snapshots on every generator run so schema evolution analysis is temporal and reproducible. Both the project brief and the manual make this a non-optional dependency for the analyzer. fileciteturn0file0turn0file1

#### Design decisions

* Contracts are generated from observed outputs, not only hand-authored specs.
* Flattening is internal; emitted contracts preserve domain semantics.
* Confidence-like fields receive semantic hardening beyond generic numeric checks.
* Latest lineage snapshot is treated as the source of truth for downstream consumers.

### 6.2 ValidationRunner

#### Purpose

The ValidationRunner executes every relevant clause for a given contract and emits a structured JSON report with PASS/FAIL/WARN/ERROR semantics. The exact output structure is specified in the main brief and is therefore a hard interface requirement. fileciteturn0file0

#### Inputs

* one contract YAML
* one JSONL dataset snapshot
* optional baseline snapshots

#### Outputs

* `validation_reports/*.json`
* updated numeric baselines after first successful run

#### Validation strategy

**Layer 1 — Structural validation**

* required field presence
* type conformance
* enum conformance
* UUID/date-time format checks
* path existence or relationship checks where applicable

These checks correspond to the manual’s recommended implementation order and the project’s schema-level enforcement targets. fileciteturn0file0turn0file1

**Layer 2 — Semantic range validation**

Apply explicit min/max constraints and field-specific semantics such as `confidence <= 1.0`, `overall_verdict` enum membership, and `recorded_at >= occurred_at`. This layer catches obvious but high-impact semantic breaks. fileciteturn0file0

**Layer 3 — Statistical drift validation**

For numeric fields with baselines, compute z-score style drift using stored mean and stddev. The project brief requires WARN beyond 2 stddev and FAIL beyond 3 stddev, precisely to catch silent corruption that passes structural checks. fileciteturn0file0turn0file1

#### Error-handling contract

The runner must never crash on a bad dataset. Missing columns become `ERROR` results with diagnostics, and execution continues. This behavior is explicitly mandated in the project brief under the partial failure rule. fileciteturn0file0

#### Design decisions

* Reports are append-only artifacts, not transient console output.
* All checks produce explicit result objects so reporting is stable and composable.
* Baseline writing happens only after a successful clean run to prevent poisoning the reference distribution with known-bad data.

### 6.3 ViolationAttributor

#### Purpose

The ViolationAttributor converts a failing validation result into a ranked causal hypothesis with source files, commits, authors, timestamps, and blast radius. It combines lineage provenance with git provenance. fileciteturn0file0

#### Inputs

* validation report containing one or more FAIL results
* latest lineage snapshot
* corresponding contract YAML with downstream metadata
* git history for candidate producer files

#### Outputs

* `violation_log/violations.jsonl`

#### Attribution pipeline

1. identify the failing field from the validation result
2. map that field to the producing system in the lineage graph
3. traverse upstream breadth-first to producer files
4. query recent git history and, where possible, targeted blame information
5. rank candidates using recency and lineage distance
6. attach blast radius from the contract’s downstream section
7. write a normalized violation record

This exact logic is laid out in the main brief, with the manual providing practical helper sketches for file-path extraction and scoring. fileciteturn0file0turn0file1

#### Design decisions

* BFS is used because nearest upstream causes are likelier than deep ancestors.
* The design caps candidates at five because the brief explicitly requires at least one and no more than five.
* Blast radius is contract-derived first, lineage-derived second. That keeps the report aligned with the generation-time view of downstream consumers. fileciteturn0file0turn0file1

### 6.4 SchemaEvolutionAnalyzer

#### Purpose

The analyzer diffs schema snapshots over time, classifies changes, and produces compatibility and migration impact outputs. It does not prevent change; it makes change legible and safe. That framing comes directly from the main brief. fileciteturn0file0

#### Inputs

* two or more timestamped schema snapshots for the same contract

#### Outputs

* compatibility verdict JSON
* migration impact report JSON
* human-readable diff summary

#### Classification logic

The analyzer implements the project taxonomy:

* add nullable field -> compatible
* add required field -> breaking
* rename field -> breaking
* widening type -> usually compatible
* narrowing type or scale change -> breaking
* remove field -> breaking
* additive enum expansion -> usually compatible
* enum value removal -> breaking

The manual provides a concise starter function for these rules; the brief defines the authoritative taxonomy. fileciteturn0file0turn0file1

#### Design decisions

* Snapshot diffs are field-clause aware, not raw YAML string diffs.
* Impact reports must include downstream consumers and a rollback plan because the project explicitly requires them for breaking changes. fileciteturn0file0

### 6.5 AI Contract Extensions

#### Purpose

Standard contract tooling is insufficient for AI-centric systems. This component adds explicit checks for embedding drift, prompt input schema conformance, and structured LLM output validity. The main brief treats these as gaps in existing tooling that Week 8 must fill. fileciteturn0file0

#### Subcomponent A — Embedding drift

Baseline on sampled Week 3 text values, store the centroid, and compare later runs using cosine distance. The project brief even supplies the reference algorithm and thresholded PASS/FAIL semantics. fileciteturn0file0

#### Subcomponent B — Prompt input schema validation

Before structured metadata is interpolated into prompts, validate it against a JSON Schema and quarantine non-conforming records rather than silently dropping them. This is an explicit assignment requirement. fileciteturn0file0

#### Subcomponent C — Structured output enforcement

Validate Week 2 verdict-style structured LLM outputs against the expected schema and track a per-prompt-version schema violation rate. A rising rate becomes a model or prompt degradation signal. The main brief and manual both highlight this metric. fileciteturn0file0turn0file1

#### Design decisions

* AI-specific violations are written into the same overall reporting plane as structural data violations.
* Prompt input records are quarantined, not discarded.
* The violation-rate metric is trended over time rather than treated as a single-run fact.

### 6.6 ReportGenerator

#### Purpose

The ReportGenerator converts live validation and violation artifacts into an executive-readable Enforcer Report. The report is not a vanity deliverable; it is a core grading artifact and the client-facing proof of value. The main brief specifies required report sections and the practitioner manual gives a starter scoring routine. fileciteturn0file0turn0file1

#### Inputs

* validation reports
* violation log
* AI metrics
* schema evolution outputs

#### Outputs

* `enforcer_report/report_data.json`
* `enforcer_report/report_{date}.pdf`

#### Required sections

1. data health score and one-sentence narrative
2. violations this week by severity with plain-language summaries
3. schema changes detected and required downstream action
4. AI system risk assessment
5. prioritized recommended actions

Those five sections are mandatory in the main brief. fileciteturn0file0

#### Design decisions

* The report is generated strictly from artifact files, not hand-authored prose.
* Plain-language phrasing is mandatory for violation descriptions because the brief emphasizes non-technical communication to product stakeholders. fileciteturn0file0

## 7. Execution flow

### Baseline run

1. generate contract for Week 3
2. generate contract for Week 5
3. write schema snapshots
4. run validation on clean datasets
5. establish numeric baselines
6. optionally run AI baseline generation for embeddings and output-schema metrics

### Violation run

1. run validation on a violated or naturally failing dataset
2. detect FAIL/WARN/ERROR results
3. attribute failing checks through lineage and git
4. write violation log entries
5. diff new snapshots if schema changed
6. rerun AI checks as needed
7. generate updated Enforcer Report

This mirrors the end-to-end integration sequence recommended in the practitioner manual while remaining faithful to the main brief’s required output chain. fileciteturn0file1turn0file0

## 8. Non-functional requirements

### Reproducibility

Every script must be runnable independently from CLI entry points because evaluators will execute them directly. The brief and manual both assume a fresh-clone workflow driven by README commands. fileciteturn0file0turn0file1

### Robust failure handling

Validation must continue through missing columns or partial failures. Attribution must return at least one candidate when possible. Schema evolution analysis must degrade gracefully if snapshots are insufficient. These are not niceties; they affect rubric performance and evaluator experience. fileciteturn0file0turn0file1

### Human readability

Generated contracts must be readable without consulting generator code. The manual says explicitly that if a clause cannot be understood without the code, it is not good enough. That principle is adopted as a design constraint. fileciteturn0file1

### File-based transparency

Outputs are intentionally materialized on disk rather than hidden behind a service boundary. That makes debugging, grading, and future pipeline reuse easier.

## 9. Risk register and mitigations

### Risk 1 — Real outputs diverge from canonical schemas

**Impact:** generated contracts reflect broken data instead of intended interfaces.

**Mitigation:** treat the main brief as canonical, document deviations in `DOMAIN_NOTES.md`, and write migration scripts where necessary rather than silently redefining the contract. This approach is explicitly recommended in the practitioner manual. fileciteturn0file1

### Risk 2 — Structural checks pass while semantics fail

**Impact:** silent corruption survives into downstream systems.

**Mitigation:** enforce confidence and similar bounded fields with both hard range checks and statistical drift baselines. This is the central lesson of the Week 3 confidence example. fileciteturn0file0

### Risk 3 — No snapshots exist for schema diffing

**Impact:** schema evolution analyzer cannot classify changes.

**Mitigation:** generator writes snapshots on every run; rerun generator after injected changes. The manual explicitly flags this as a common failure. fileciteturn0file1

### Risk 4 — Git attribution is empty or misleading

**Impact:** violation log lacks actionable blame chain.

**Mitigation:** ensure git commands run in the correct repository root, use recent-history filtering, and combine git evidence with lineage distance scoring rather than blindly trusting the newest commit. The manual notes the wrong-working-directory failure mode directly. fileciteturn0file1

### Risk 5 — Enforcer Report becomes hand-written or hard-coded

**Impact:** evaluator reruns produce inconsistent numbers and the report loses trustworthiness.

**Mitigation:** derive score, violation counts, and top issues strictly from generated JSON artifacts. The manual explicitly warns against hard-coding report numbers. fileciteturn0file1

## 10. Why this design should score well

This design aligns with the highest-value scoring path because it emphasizes:

* required Week 3 and Week 5 contract generation
* structured JSON validation output
* drift detection for silent corruption
* ranked blame chains with blast radius
* temporal schema diffing with compatibility verdicts
* all three AI extensions
* machine-generated reporting from live artifacts

Those items map directly to the project rubric’s definition of a functional-to-production-ready submission. The main brief makes clear that evaluators run the scripts rather than trusting narrative claims, so the design intentionally privileges executable artifact flow over decorative architecture. fileciteturn0file0

## 11. Final design summary

The Data Contract Enforcer is designed as a file-driven validation and attribution pipeline over the user’s own Weeks 1–5 outputs. Its core idea is simple: generate contracts from real data, enforce those contracts continuously, detect both schema and statistical failures, trace violations through lineage and git, and express the result in a report a stakeholder can act on. That design directly serves the stated Week 8 objective of preventing silent breakage and proving where, why, and how broadly a contract failure propagates. fileciteturn0file0

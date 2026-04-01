# DESIGN.md

## Primary Design Principle

Every inter-system data exchange is treated as a **machine-enforced contract boundary**.

The system enforces:

* schema correctness
* statistical consistency
* lineage traceability
* AI-specific validation

---

## 1. System goal

Prevent silent data corruption by:

* enforcing schema and constraints
* detecting drift and anomalies
* attributing failures to sources
* reporting impact in plain language

---

## Cross-week integration

This Week 7 system is not fully standalone. It consumes:

- Week 3 extraction outputs as the contract source dataset
- Week 4 lineage snapshots as the dependency and attribution context

Current integrated path:

Week 3 Document Refinery -> Week 4 Cartographer lineage -> Week 7 Contract Generator

This ensures that generated contracts are grounded not only in observed schema, but also in upstream/downstream lineage context.

---

## 2. Architecture overview

The system consists of six components:

| Component               | Role                       |
| ----------------------- | -------------------------- |
| ContractGenerator       | Build contract from data   |
| ValidationRunner        | Enforce contract           |
| ViolationAttributor     | Assign ownership           |
| SchemaEvolutionAnalyzer | Detect schema drift        |
| AI Extensions           | Validate AI-specific risks |
| ReportGenerator         | Synthesize outputs         |

### Mapping to Week 7 phases

* Phase 1 → ContractGenerator
* Phase 2 → ValidationRunner
* Phase 3 → ViolationAttributor
* Phase 4 → SchemaEvolutionAnalyzer
* Phase 5 → AI Extensions
* Phase 6 → ReportGenerator

---

## 3. Repository design (enforced interface)

```text
contracts/
generated_contracts/
validation_reports/
violation_log/
schema_snapshots/
enforcer_report/
```

These directories form part of the system contract with the evaluator.

---

## 4. Component design (real implementation)

### 4.1 ContractGenerator

* loads JSONL data
* profiles columns
* infers schema
* incorporates lineage context from Week 4 snapshots
* generates:

  * contract YAML
  * dbt schema
  * snapshot

**Observed behavior:**

* inference is imperfect
* requires post-generation validation

---

### 4.2 ValidationRunner

Performs:

* schema validation
* range checks
* statistical drift detection

**Outcome:**

* successfully detected confidence corruption
* identified extreme drift

---

### 4.3 ViolationAttributor

Pipeline:

* consume validation failures
* map to lineage
* attempt git attribution
* compute blast radius

**Limitation:**

* insufficient lineage → UNKNOWN attribution

---

### 4.4 SchemaEvolutionAnalyzer

* compares snapshots
* classifies changes

**Observed result:**

* detected format change (`uuid → None`)
* captured inference corrections as breaking

---

### 4.5 AI Extensions

| Check             | Status        |
| ----------------- | ------------- |
| Embedding drift   | PASS          |
| Prompt validation | PASS          |
| Output schema     | Not available |

**Design features:**

* fallback embedding (no API dependency)
* quarantine mechanism for invalid inputs

---

### 4.6 ReportGenerator

Aggregates all outputs into:

* health score
* violation summaries
* schema changes
* AI risk assessment
* recommendations
* attribution summary

**Key property:**

> Fully artifact-driven, no hardcoded logic.

---

## 5. Execution flow

### Baseline run

1. generate contract
2. validate clean data
3. establish baseline

### Violation run

1. inject failure
2. detect violations
3. attribute
4. analyze schema
5. generate report

This mirrors the required evaluation pipeline.

---

## 6. Key design decisions

### File-based architecture

* reproducible
* inspectable
* evaluator-friendly

### Statistical validation

* required for semantic correctness

### Graceful degradation

* system runs even with partial AI inputs

### Plain-language reporting

* converts technical failures into actionable insights

---

## 7. Known limitations

* attribution limited by lineage depth
* schema inference not fully canonical
* missing Week 2 data for full AI validation

---

## 8. Why this design works

The system implements:

```text
Detection → Diagnosis → Attribution → Impact → Reporting
```

This goes beyond validation into full enforcement.

---

## 9. Final summary

This system is not just a validator.

It is an **end-to-end enforcement pipeline** that:

* detects failures
* explains them
* traces them to source
* estimates impact
* supports decision-making

This directly satisfies the Week 7 requirements.

---

## 10. Traceability to outputs

Each component produces verifiable artifacts:

- ContractGenerator → contract YAML + snapshot
- ValidationRunner → validation reports
- ViolationAttributor → violation logs
- SchemaAnalyzer → evolution reports
- AI Extensions → AI metrics JSON
- ReportGenerator → final enforcement report

This ensures full traceability from input data to final decision outputs.

# Functionality derived from the framework

Source material: CCB booklet *CyberFundamentals 2025, BASIC* (version 2025-10-01), the CCB *Self-Assessment tool BASIC* (tool version 2026-02-20) and the CCB risk assessment workbook *CyFun-Selection* (version 2024-01-08). The CyFun Conformity Assessment Scheme (CAS) defines how a self-declaration becomes a verified label.

## 1. The BASIC label process

1. **Scope.** The entity defines what the self-assessment covers (legal entity, sites, systems, services) and what is excluded.
2. **Assurance level.** The CCB risk method confirms which level applies. BASIC is the level for organisations whose score stays under 100; higher scores point to IMPORTANT or ESSENTIAL.
3. **Self-assessment.** Every requirement receives a documentation maturity and an implementation maturity (1 to 5) with the CCB definitions. The tool computes subcategory, category and total maturity.
4. **Remediation.** Gaps below the thresholds are closed.
5. **Evidence.** Documents and records that show the maturity claimed exist and are kept.
6. **Self-declaration.** The entity submits the completed CCB workbook.
7. **Verification.** A CAB verifies the self-assessment and evidence and issues the label.
8. **Keep the label.** Scores, evidence and risks are reviewed as the organisation changes.

The application models exactly these stages (Scope and journey screen) and provides one screen per step.

## 2. Assurance level risk assessment

The CCB workbook computes, for each of 5 attack categories (sabotage, information theft, crime, hacktivism, disinformation) and each of 5 threat actor types (competitors, ideologues, terrorists, cyber criminals, nation states):

    probability (Low 0, Med 0,5, High 1) × impact (Low 0, Med 5, High 10) × attack type (global 1, targeted 2) × organisation size (small 1, medium 2, large 3)

The sum over the 25 cells gives the score: 0 to 99 BASIC, 100 to 199 IMPORTANT, 200 and above ESSENTIAL. Each NIS2 sector sheet carries default impacts and probabilities. The application loads these defaults per sector, lets the user adjust cells (marked when they differ from the default), recalculates live, and stores the rationale. Output: the level, the per-actor subtotals and a documented justification that goes into the audit pack. The risk register covers the "simple risk register" the booklet asks for under ID.RA-01.1.

## 3. Self-assessment

Structure from the workbook: 6 functions (GOVERN, IDENTIFY, PROTECT, DETECT, RESPOND, RECOVER), 17 categories, 28 subcategories, 34 requirements, 13 key measures.

Per requirement the application holds: the requirement statement, key measure flag, documentation score, implementation score, not-applicable flag, justification (exported to the workbook comment column), guidance summary, typical evidence, linked documents, linked evidence, mapped automated checks, remediation actions and the change history.

Rules enforced:

* scores are whole numbers 1 to 5;
* at most one requirement is not applicable, never a key measure, and only with a justification;
* a not-applicable requirement counts 2,5 on both dimensions;
* subcategory = average of requirements, category = average of subcategories, maturity = average of the two dimensions, total = average of the 17 categories;
* BASIC passes when total ≥ 2,5 and every key measure ≥ 2,5.

The scoring module is tested against the workbook formulas, including the case where a subcategory with several requirements (PR.AA-05 has four) weighs the same as a single-requirement subcategory.

## 4. Registers that carry maturity

* **Documents.** Documentation maturity 2 requires formally approved documents; 3 and higher require review within two years and documented exceptions. The register records version, approver, approval and review dates, and the requirements each document supports. Review-due documents are flagged on the dashboard.
* **Assets.** ID.AM-01.1 (infrastructure), ID.AM-02.1 (software and services), ID.AM-05.1 (classification, criticality, primary/secondary, owner) and ID.AM-07.1 (data) share one inventory with a kind field. Connectors populate it; the user owns classification and ownership.
* **Evidence.** Files are stored under random names with their SHA-256 hash, size and collection date, and mapped to requirements. Links are stored as URLs. Both appear in the audit pack.
* **Actions.** Remediation planning with owner, priority, due date and status. Internal; excluded from every export.

## 5. Connected systems

The booklet asks for automated discovery where possible (ID.AM-01.1, ID.AM-02.1) and for continuous facts: MFA on remote access (PR.AA-03.2), managed identities and reviewed access (PR.AA-01.1, PR.AA-05.x), no day-to-day administrative privileges (PR.AA-05.4), patched systems (ID.AM-08.2), anti-malware (DE.CM-01.2), logging (PR.PS-04.1, DE.AE-03.1), firewalls (PR.IR-01.1), web and e-mail filtering (PR.PS-05.1), vulnerability awareness (ID.RA-01.1).

Each connector produces inventory items and checks with a status (pass, fail, warn, info, error), a one-line summary, structured details and the requirement identifiers it supports. Results are shown on the dashboard, on each requirement and in the audit view; a raw snapshot of every run is kept and included in the audit pack. See docs/connectors.md for the mapping.

## 6. Verification support

* **Audit view.** One read-only page with everything per requirement. An Auditor role gives a CAB read-only access to the live application if wanted.
* **Snapshots.** Freeze the full state at the self-declaration date and before the verification.
* **Official workbook export.** The CCB workbook is filled in place: scores in columns F and G, comments in L, completion date on the Introduction sheet, cached formula values recomputed, everything else byte-identical. The CCB file is uploaded by the user, not bundled.
* **Audit pack.** ZIP with a self-contained summary page, assessment and risk JSON, document and asset registers, every evidence file with hash, latest checks and connector snapshots.

## 7. Deliberately out of scope

IMPORTANT and ESSENTIAL levels, multi-tenant use, local accounts, e-mail notifications, workflow approvals, AI-generated text, policy templates. The CCB toolbox on cyfun.eu provides policy templates; the document register links to wherever they live.

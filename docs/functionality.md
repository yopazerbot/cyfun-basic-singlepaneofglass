# Functionality derived from the framework

Source material: CCB booklets *CyberFundamentals 2025* for BASIC, IMPORTANT and ESSENTIAL (version 2025-10-01), the CCB self-assessment tools for the three levels (BASIC and IMPORTANT tool version 2026-02-20, ESSENTIAL v3.1 of 2026-02-25), the CCB key measures document and the CCB risk assessment workbook *CyFun-Selection* (version 2024-01-08). The CyFun Conformity Assessment Scheme (CAS) defines how a self-declaration becomes a verified label.

## 1. The label process

1. **Scope and level.** The entity defines what the self-assessment covers (legal entity, sites, systems, services), what is excluded, and which assurance level it pursues.
2. **Assurance level.** The CCB risk method confirms which level applies: scores under 100 point to BASIC, 100 to 199 to IMPORTANT, 200 and above to ESSENTIAL.
3. **Self-assessment.** Every requirement of the level receives a documentation maturity and an implementation maturity (1 to 5) with the CCB definitions. The tool computes subcategory, category and total maturity.
4. **Remediation.** Gaps below the thresholds are closed.
5. **Evidence.** Documents and records that show the maturity claimed exist and are kept.
6. **Self-declaration.** The entity submits the completed CCB workbook of its level.
7. **Verification.** A CAB verifies the self-assessment and evidence and issues the label.
8. **Keep the label.** Scores, evidence and risks are reviewed as the organisation changes; at ESSENTIAL the controls linked to management aspects are reviewed at every audit.

The application models exactly these stages (Scope, level and journey screen) and provides one screen per step.

## 2. Assurance level risk assessment

The CCB workbook computes, for each of 5 attack categories (sabotage, information theft, crime, hacktivism, disinformation) and each of 5 threat actor types (competitors, ideologues, terrorists, cyber criminals, nation states):

    probability (Low 0, Med 0,5, High 1) × impact (Low 0, Med 5, High 10) × attack type (global 1, targeted 2) × organisation size (small 1, medium 2, large 3)

The sum over the 25 cells gives the score. Each NIS2 sector sheet carries default impacts and probabilities. The application loads these defaults per sector, lets the user adjust cells (marked when they differ from the default), recalculates live, compares the result with the target level and stores the rationale. The risk register covers the "simple risk register" the booklet asks for under ID.RA-01.1.

## 3. Self-assessment

| | BASIC | IMPORTANT | ESSENTIAL |
|---|---|---|---|
| Functions / categories / subcategories | 6 / 17 / 28 | 6 / 20 / 66 | 6 / 22 / 95 |
| Requirements | 34 | 133 | 218 |
| Key measures | 13 | 22 | 29 |
| Controls linked to management aspects | 1 | 10 | 16 |

Each requirement carries the level that introduces it (Basic, Important, Essential). The IMPORTANT tool contains the Basic and Important requirements; the ESSENTIAL tool contains all three. The target level selects the requirement set, the thresholds and the N/A rules; scores are stored per requirement identifier and survive a change of level.

Per requirement the application holds: the requirement statement, level, key measure and management-aspect flags, documentation score, implementation score, not-applicable flag, justification (exported to the workbook comment column), the CCB goal statement, guidance and typical evidence (BASIC requirements), linked documents, linked evidence, mapped automated checks, remediation actions and the change history.

Rules enforced per level:

* scores are whole numbers 1 to 5;
* at most 1 / 3 / 5 requirements are not applicable, never a key measure, and at ESSENTIAL never a control linked to management aspects; a justification is required;
* a not-applicable requirement counts 2,5 (BASIC) or 3 (IMPORTANT, ESSENTIAL) on both dimensions;
* subcategory = average of requirements, category = average of subcategories, maturity = average of the two dimensions, total = average of the categories;
* the level passes when total ≥ 2,5 / 3 / 3,5, every key measure ≥ 2,5 / 3 / 3, and at ESSENTIAL every category ≥ 3.

The scoring module reproduces the workbook formulas of the three levels. The aggregation groups behind each category score are parsed from the workbook's own category formulas, so the application shows the same numbers Excel computes in the submitted file. The CCB workbooks deviate from their subcategory structure in a few places, and the application follows the workbook there (the Self-assessment page lists them): at IMPORTANT and ESSENTIAL, PR.IR-04.1 is averaged together with the previous subcategory; at ESSENTIAL, GV.OV-02.1 and GV.OV-03.1 are averaged as one group, RC.CO-04.1 to RC.CO-04.3 each count as a group of their own, and the implementation average of PR.AA leaves out PR.AA-06. The tests check all of this against the real workbooks when they are present next to the repository. The CCB IMPORTANT and ESSENTIAL workbooks list only the 13 BASIC key measures in their summary tables; the application evaluates all requirements flagged as key measures in the function sheets (22 and 29), which matches the CCB key measures document.

## 4. Registers that carry maturity

* **Documents.** Documentation maturity 2 requires formally approved documents; 3 and higher require review within two years and documented exceptions. The register records version, approver, approval and review dates, and the requirements each document supports. Review-due documents are flagged on the dashboard.
* **Assets.** ID.AM-01.1 (infrastructure), ID.AM-02.1 (software and services), ID.AM-05.1 (classification, criticality, primary/secondary, owner) and ID.AM-07.1 (data) share one inventory with a kind field. Connectors populate it; the user owns classification and ownership.
* **Evidence.** Files are stored under random names with their SHA-256 hash, size and collection date, and mapped to requirements. Links are stored as URLs. Every connector check result is kept as automated evidence that points at the run's snapshot and its SHA-256 hash, refreshed by every run. All three appear in the audit pack.
* **Actions.** Remediation planning with owner, priority, due date and status. Internal; excluded from every export.

## 5. Connected systems

The booklets ask for automated discovery where possible (ID.AM-01.1, ID.AM-02.1) and for continuous facts: MFA on remote access (PR.AA-03.2), managed identities and reviewed access (PR.AA-01.1, PR.AA-05.x), no day-to-day administrative privileges (PR.AA-05.4), patched systems (ID.AM-08.2), anti-malware (DE.CM-01.2), logging (PR.PS-04.1, DE.AE-03.1), firewalls (PR.IR-01.1), web and e-mail filtering (PR.PS-05.1), vulnerability awareness (ID.RA-01.1).

Each connector produces inventory items and checks with a status (pass, fail, warn, info, error), a one-line summary, structured details and the requirement identifiers it supports. Results are shown on the dashboard, on each requirement and in the audit view, and each check is registered as automated evidence; a raw snapshot of every run is kept and included in the audit pack. Credentials are entered on the Settings page (encrypted) or set as environment variables. See docs/connectors.md for the mapping.

## 6. Verification support

* **Audit view.** One read-only page with everything per requirement. An Auditor role (Entra app role or a local account) gives a CAB read-only access to the live application if wanted.
* **Snapshots.** Freeze the full state, with its level, at the self-declaration date and before the verification.
* **Official workbook export.** The CCB workbook of the target level is filled in place: scores, comments, completion date, cached formula values recomputed, everything else byte-identical. The CCB file is uploaded by the user, not bundled; a workbook of another level is refused.
* **Audit pack.** ZIP with a self-contained summary page, assessment and risk JSON, document and asset registers, every evidence file with hash, latest checks and connector snapshots.

## 7. Claude-assisted scoring (optional)

Translating connector results, documents and evidence into the CCB maturity scale is the slow part of the self-assessment. With an Anthropic API key on the Settings page, Claude proposes the documentation and implementation score of a requirement with a justification, the cited material, the gaps to the next level and remediation actions. Guard rules hold proposals to the CCB definitions where the material allows a mechanical check, and an administrator accepts, edits or rejects every proposal; nothing changes a score on its own. Names, e-mail addresses and devices are replaced by placeholders before sending. See docs/ai-assistance.md.

## 8. Deliberately out of scope

Multi-tenant use, e-mail notifications, workflow approvals, automatic score changes, policy templates. The CCB toolbox on cyfun.eu provides policy templates; the document register links to wherever they live.

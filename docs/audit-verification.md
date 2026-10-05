# Self-declaration and verification

## Before the self-declaration

1. **Scope, level and journey**: organisation, legal entity, enterprise number, scope statement, exclusions, CAB, self-assessment completion date, target assurance level.
2. **Risk assessment**: sector, size, matrix, rationale saved. If the method points to a different level than the target, record why in the rationale (for example BASIC as a first step towards IMPORTANT).
3. **Self-assessment**: all requirements of the level scored, N/A within the level's limit and never on key measures (at ESSENTIAL also never on management-aspect controls), a justification on every requirement that states where the documentation lives and how implementation is evidenced.
4. **Dashboard**: total maturity at or above the level's threshold, all key measures at or above theirs, at ESSENTIAL every category ≥ 3, no rule problems shown.
5. **Documents and evidence**: every requirement has at least one approved document or one evidence item. The dashboard shows the coverage percentage.
6. **Connected systems**: run every connector, look at every `fail`, decide and document.
7. **Snapshot**: Audit view → Take snapshot, named for example "Self-declaration 2026-10-05". The snapshot records the level.
8. **Export the CCB workbook**: Audit view and export → Export → upload your downloaded copy of the CCB tool of your level → download the filled file. Open it in Excel, check the Summary sheet, save, and submit it as the CCB process requires.
9. **Export the audit pack** and store it with the submitted workbook.

## During the verification

* Give the CAB auditor the audit pack, or an Auditor-role account for read-only access to the live application: an Entra app role assignment, or a local account created on the Users page (temporary password, forced change). Remove it after the verification.
* The Audit view answers each requirement in one block: statement, level, flags, scores, justification, evidence with hashes, documents with approval dates, latest automated checks with timestamps.
* Snapshots show what was declared on the self-assessment date versus the state at verification.
* Connector snapshots (JSON) show raw facts at run time; the auditor can compare them with the live systems.
* Remediation actions, journey notes and the activity log are internal and not in the pack. Share them only if you want to.

## What the CCB workbook export changes, and what it leaves alone

| Written | Left untouched |
|---|---|
| Documentation and implementation scores per requirement, or the text `N/A` (columns F/G at BASIC, G/H at IMPORTANT and ESSENTIAL) | sheet protection, data validation lists, conditional formatting (including the N/A count warning), the assurance level filter column |
| Justification into "Comments and/or additional information" (column L at BASIC, M at IMPORTANT and ESSENTIAL) | "Assessor comments", reserved for the CAB |
| Self-assessment completion date on the Introduction sheet | tool version, change log, references, maturity level definitions |
| Cached values of formula cells (subcategory, category, summary, key measures), recomputed with the workbook's own formulas | the formulas themselves, the summary chart, styles, shared strings, custom XML parts |
| `fullCalcOnLoad` flag so Excel recalculates on open | everything else, byte for byte |

The export validates the uploaded file: all sheets of that level's tool must exist and every requirement row must carry the expected requirement identifier. A different tool version or the workbook of another level is refused with a message.

## After the label

Keep scoring honest: when a document is reviewed or a control changes, update the score, the justification and the evidence. Take a snapshot at least yearly and before any surveillance by the CAB. At ESSENTIAL, the controls linked to management aspects (tag MA) are reviewed at every audit. The connectors keep producing dated facts in between.

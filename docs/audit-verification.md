# Self-declaration and verification

## Before the self-declaration

1. **Scope and journey**: organisation, legal entity, enterprise number, scope statement, exclusions, CAB, self-assessment completion date.
2. **Risk assessment**: sector, size, matrix, rationale saved. The result should read BASIC; if it reads IMPORTANT or ESSENTIAL, record why BASIC is pursued now (for example as a first step) in the rationale.
3. **Self-assessment**: all 34 requirements scored, one N/A at most and never on a key measure, a justification on every requirement that states where the documentation lives and how implementation is evidenced.
4. **Dashboard**: total maturity ≥ 2,50, 13 of 13 key measures ≥ 2,50, no rule problems shown.
5. **Documents and evidence**: every requirement has at least one approved document or one evidence item. The dashboard shows the coverage percentage.
6. **Connected systems**: run every connector, look at every `fail`, decide and document.
7. **Snapshot**: Audit view → Take snapshot, named for example "Self-declaration 2026-10-05".
8. **Export the CCB workbook**: Audit view and export → Export → upload your downloaded copy of the CCB BASIC tool → download the filled file. Open it in Excel, check the Summary sheet, save, and submit it as the CCB process requires.
9. **Export the audit pack** and store it with the submitted workbook.

## During the verification

* Give the CAB auditor the audit pack, or an Auditor-role account for read-only access to the live application (assign the Entra app role, remove it after the verification).
* The Audit view answers each requirement in one block: statement, scores, justification, evidence with hashes, documents with approval dates, latest automated checks with timestamps.
* Snapshots show what was declared on the self-assessment date versus the state at verification.
* Connector snapshots (JSON) show raw facts at run time; the auditor can compare them with the live systems.
* Remediation actions, journey notes and the activity log are internal and not in the pack. Share them only if you want to.

## What the CCB workbook export changes, and what it leaves alone

| Written | Left untouched |
|---|---|
| Documentation score (column F) and implementation score (column G) per requirement, or the text `N/A` | sheet protection, data validation lists, conditional formatting (including the N/A count warning) |
| Justification into "Comments and/or additional information" (column L) | "Assessor comments" (column M), reserved for the CAB |
| Self-assessment completion date (Introduction!T27) | tool version, change log, references, maturity level definitions |
| Cached values of formula cells (subcategory, category, summary, key measures), recomputed with the workbook's own formulas | the formulas themselves, the summary chart, styles, shared strings, custom XML parts |
| `fullCalcOnLoad` flag so Excel recalculates on open | everything else, byte for byte |

The export validates the uploaded file: all sheets must exist and every requirement row must carry the expected requirement identifier. A different tool version is refused with a message.

## After the label

Keep scoring honest: when a document is reviewed or a control changes, update the score, the justification and the evidence. Take a snapshot at least yearly and before any surveillance by the CAB. The connectors keep producing dated facts in between.

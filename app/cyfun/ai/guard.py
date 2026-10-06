"""Checks on Claude's answer before anyone sees it.

1. Structure: the answer must match the schema (scores 1 to 5, known enums).
2. Citations: every ref must exist in the packet, and every quote must appear in that item.
   A reference to an unknown item is removed; a quote that is not in the item is removed.
3. CCB limits, strict on the hook in the maturity definitions:
   * documentation 2 or higher needs an approved document linked to the requirement;
   * documentation 3 or higher needs one reviewed or approved within the previous 2 years;
   * implementation 3 or higher needs at least one evidence item or one passing check.
   A proposal above a limit is lowered to it, and the reason is shown next to the score.
4. Placeholders are translated back to the original names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .packet import normalise
from .pseudonym import Pseudonymizer

QUOTE_LIMIT = 200
JUSTIFICATION_LIMIT = 900


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Dimension(_Strict):
    score: int = Field(ge=1, le=5)
    rationale: str


class Reference(_Strict):
    ref: str
    quote: str
    supports: Literal["documentation", "implementation", "both"]


class Gap(_Strict):
    dimension: Literal["documentation", "implementation"]
    next_level: int = Field(ge=2, le=5)
    missing: str


class ProposedAction(_Strict):
    title: str
    reason: str


class Answer(_Strict):
    documentation: Dimension
    implementation: Dimension
    confidence: Literal["high", "medium", "low"]
    justification: str
    references: list[Reference]
    gaps: list[Gap]
    evidence_to_collect: list[str]
    proposed_actions: list[ProposedAction]
    contradictions: list[str]
    notes_for_reviewer: str


@dataclass
class Checked:
    doc: int
    impl: int
    confidence: str
    justification: str
    result: dict


def check(data: dict, refs: dict, facts: dict, pseudo: Pseudonymizer) -> Checked:
    """Validate and limit an answer. Raises pydantic.ValidationError when the structure is wrong."""
    a = Answer.model_validate(data)
    notes: list[str] = []
    references = []
    for r in a.references:
        key = r.ref.strip().upper()
        item = refs.get(key)
        if item is None:
            notes.append(f"Reference {r.ref.strip()[:20]} does not exist in the material and was removed.")
            continue
        quote = r.quote.strip()[: QUOTE_LIMIT + 20]
        verified = bool(quote) and normalise(quote) in normalise(item["text"])
        if quote and not verified:
            notes.append(f"The quote given for {key} does not appear in that item and was removed.")
        references.append(
            {"ref": key, "kind": item["kind"], "title": item["title"], "link": item["link"], "quote": quote if verified else "", "supports": r.supports}
        )

    doc, impl = a.documentation.score, a.implementation.score
    caps = []

    def cap(dimension: str, proposed: int, applied: int, reason: str) -> int:
        caps.append({"dimension": dimension, "proposed": proposed, "applied": applied, "reason": reason})
        return applied

    if doc >= 2 and facts.get("approved_documents", 0) == 0:
        doc = cap(
            "documentation",
            doc,
            1,
            "No approved document in the register is linked to this requirement. CCB level 2 needs formally approved documentation.",
        )
    elif doc >= 3 and facts.get("recently_reviewed_documents", 0) == 0:
        doc = cap(
            "documentation",
            doc,
            2,
            "No linked approved document was reviewed or approved in the previous 2 years. CCB level 3 needs a review within 2 years.",
        )
    if impl >= 3 and facts.get("evidence_items", 0) == 0 and facts.get("passing_checks", 0) == 0:
        impl = cap(
            "implementation",
            impl,
            2,
            "No evidence item and no passing automated check is linked to this requirement. CCB level 3 needs evidence for most activities.",
        )
    if len(a.justification) > JUSTIFICATION_LIMIT * 1.5:
        notes.append(f"The justification has {len(a.justification)} characters; shorten it before the export.")

    result = {
        "proposed": {"documentation": a.documentation.score, "implementation": a.implementation.score},
        "rationale": {"documentation": a.documentation.rationale, "implementation": a.implementation.rationale},
        "references": references,
        "gaps": [g.model_dump() for g in sorted(a.gaps, key=lambda g: (g.dimension, g.next_level))],
        "evidence_to_collect": a.evidence_to_collect,
        "proposed_actions": [x.model_dump() for x in a.proposed_actions],
        "contradictions": a.contradictions,
        "notes_for_reviewer": a.notes_for_reviewer,
        "caps": caps,
        "guard_notes": notes,
    }
    return Checked(doc, impl, a.confidence, pseudo.restore(a.justification.strip()), pseudo.restore(result))

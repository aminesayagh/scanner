"""Compare proposed sections with a manual annotation (milestone 7 evaluation).

An annotation lists the expected sections top to bottom, each anchored by texts that appear inside it.
Anchors avoid pixel-exact labelling: a boundary is missed when two neighbouring expected sections share
a proposed section, and a false split happens when one expected section's anchors land in different
proposed sections.
"""

from pydantic import BaseModel, Field

from .models import ElementSnapshot, ProposalSnapshot


class Annotation(BaseModel):
    page: str
    annotator: str  # who decided the expected sections, and how
    sections: list[list[str]] = Field(min_length=1)  # anchor texts per expected section, top to bottom


class Evaluation(BaseModel):
    expected_sections: int
    proposed_sections: int  # header/footer/flow; overlays are reviewed separately
    missed_boundaries: list[str]
    false_splits: list[str]
    ambiguous_sections: int
    unresolved_anchors: list[str]  # anchors not found in the capture: fix the annotation
    estimated_edits: int  # merges + splits to fix, plus ambiguous sections to inspect


def evaluate(elements: ElementSnapshot, props: ProposalSnapshot, ann: Annotation) -> Evaluation:
    owner = {e: s.id for s in props.sections for e in s.element_ids}
    unresolved: list[str] = []

    def located(anchor: str) -> str | None:
        for e in elements.elements:
            if anchor in e.text and e.id in owner:
                return owner[e.id]
        unresolved.append(anchor)
        return None

    placed = [[located(a) for a in anchors] for anchors in ann.sections]
    false_splits = [
        f"expected section {i + 1} ({anchors[0]!r}) spans {sorted(set(filter(None, ids)))}"
        for i, (anchors, ids) in enumerate(zip(ann.sections, placed))
        if len(set(filter(None, ids))) > 1
    ]
    missed = []
    for i in range(len(placed) - 1):
        last, first = [x for x in placed[i] if x], [x for x in placed[i + 1] if x]
        if last and first and last[-1] == first[0]:
            missed.append(f"between {ann.sections[i][-1]!r} and {ann.sections[i + 1][0]!r} (both in {first[0]})")
    proposed = [s for s in props.sections if s.role != "overlay"]
    ambiguous = sum(s.ambiguous for s in proposed)
    return Evaluation(
        expected_sections=len(ann.sections),
        proposed_sections=len(proposed),
        missed_boundaries=missed,
        false_splits=false_splits,
        ambiguous_sections=ambiguous,
        unresolved_anchors=unresolved,
        estimated_edits=len(missed) + len(false_splits) + ambiguous,
    )

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 1


class Viewport(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Point(BaseModel):
    x: float
    y: float


class Browser(BaseModel):
    name: str
    version: str
    headless: bool


class Readiness(BaseModel):
    """Outcome of each bounded wait. False means the wait timed out, not that the page is broken."""

    manual: bool
    fonts_ready: bool | None = None
    images_ready: bool | None = None
    layout_stable_before_scroll: bool | None = None
    layout_stable_before_screenshot: bool | None = None


class ScrollPass(BaseModel):
    steps: int = Field(ge=0)
    reached_bottom: bool
    truncated_reason: Literal["max_steps", "max_seconds", "max_height", "stalled"] | None
    page_height: float = Field(ge=0)


class Screenshot(BaseModel):
    path: str
    # Screenshot pixels and CSS pixels differ by device_scale_factor; keep both.
    width_px: int = Field(ge=0)
    height_px: int = Field(ge=0)
    animations_frozen: bool
    attempts: int = Field(ge=1)
    layout_consistent: bool


class Manifest(BaseModel):
    schema_version: int = SCHEMA_VERSION
    capture_id: str
    status: Literal["complete", "incomplete", "failed"]
    error: str | None = None
    input_url: str
    final_url: str | None = None
    http_status: int | None = None
    captured_at: datetime
    viewport: Viewport
    device_scale_factor: float = Field(gt=0, allow_inf_nan=False)
    scroll_reference: Point | None = None
    browser: Browser | None = None
    extractor_version: str | None = None
    readiness: Readiness
    scroll: ScrollPass | None = None
    screenshot: Screenshot | None = None


class Rect(BaseModel):
    """CSS pixels."""

    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    width: float = Field(ge=0, allow_inf_nan=False)
    height: float = Field(ge=0, allow_inf_nan=False)


class Visibility(BaseModel):
    css_visibility: str
    opacity: float = Field(ge=0, le=1)
    transparent_ancestor: bool  # an ancestor has opacity 0, so this element paints nothing
    collapsed_ancestor: bool  # a clipping ancestor has zero width or height (e.g. closed accordion)
    in_scroll_container: bool  # inside a nested scroller: what shows depends on its scroll position
    has_box: bool  # non-zero width and height
    tiny: bool  # at most 1x1 CSS px, the usual visually-hidden pattern
    offscreen: bool  # rendered, but entirely outside the document area (e.g. "sr-only" tricks)
    visible: bool  # all of the above allow it to be seen


class Element(BaseModel):
    id: str
    parent_id: str | None
    child_ids: list[str]
    tag: str
    role: str | None  # explicit role attribute only; landmark tags are read from `tag`
    text: str  # whitespace-collapsed own text (not descendants'), truncated
    box: Literal["normal", "contents"]  # "contents": no own box; bounds are the union of its content
    # Fixed/sticky elements are measured where they sat at capture; see `positioning`.
    bounds: Rect  # document space
    viewport_bounds: Rect  # viewport space at the scroll reference
    positioning: Literal["flow", "absolute", "sticky", "fixed"]  # fixed/sticky include inherited from ancestors
    styles: dict[str, str]
    visibility: Visibility
    limitations: list[Literal["iframe", "embed", "canvas", "shadow_root", "scroll_container"]]


class ElementSnapshot(BaseModel):
    schema_version: int = SCHEMA_VERSION
    capture_id: str
    extractor_version: str
    scroll: Point
    document: Rect
    elements: list[Element]

    @model_validator(mode="after")
    def _check_references(self) -> "ElementSnapshot":
        _check_tree(self.elements)
        return self


def _check_tree(nodes: list) -> None:
    """Unique IDs, and parent/child references that agree in both directions."""
    by_id = {n.id: n for n in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("duplicate IDs")
    for n in nodes:
        if n.parent_id is not None and n.id not in by_id.get(n.parent_id, n).child_ids:
            raise ValueError(f"{n.id}: parent {n.parent_id} missing or does not list it as a child")
        for c in n.child_ids:
            if c not in by_id or by_id[c].parent_id != n.id:
                raise ValueError(f"{n.id}: child {c} missing or has another parent")


class Background(BaseModel):
    own: str  # "transparent", a CSS colour, or "complex" (image/gradient: no single colour)
    context_element_id: str | None  # nearest painting ancestor element; None means the page canvas
    context: str | None  # that ancestor's colour, or "complex"


class GroupEvidence(BaseModel):
    landmark: str | None  # role attribute or landmark tag
    display: str
    padding: bool
    border: bool
    background: Background
    repeated_children: bool  # 2+ same-tag, same-width child containers, not all in one column (cards)


class VisualGroup(BaseModel):
    """An inferred grouping, not a claim that the page has exactly this HTML container."""

    id: str
    parent_id: str | None
    child_ids: list[str]
    kind: Literal["container", "content", "decorative"]
    primary_element_id: str  # the element whose box defines the group
    element_ids: list[str]  # every source element this group stands for
    bounds: Rect
    positioning: Literal["flow", "absolute", "sticky", "fixed"]
    evidence: GroupEvidence
    reasons: list[str]  # why this group was kept, and which wrappers it absorbed


class Omitted(BaseModel):
    element_id: str
    reason: Literal["not_visible", "pass_through", "empty_box"]


class GroupSnapshot(BaseModel):
    schema_version: int = SCHEMA_VERSION
    capture_id: str
    normalizer_version: str
    groups: list[VisualGroup]
    omitted: list[Omitted]

    @model_validator(mode="after")
    def _check(self) -> "GroupSnapshot":
        _check_tree(self.groups)
        for g in self.groups:
            if g.primary_element_id not in g.element_ids:
                raise ValueError(f"{g.id}: primary element {g.primary_element_id} is not among its members")
        members = [i for g in self.groups for i in g.element_ids] + [o.element_id for o in self.omitted]
        if len(members) != len(set(members)):
            raise ValueError("an element is assigned more than once")
        return self


class RegionCandidate(BaseModel):
    """A possible section: one visual group, or side-by-side siblings that share a row."""

    id: str
    role: Literal["header", "footer", "flow", "overlay"]
    level: int = Field(ge=0)  # 0: top-level candidate; N + 1: nested alternative inside a level-N region
    parent_region_id: str | None
    group_ids: list[str]
    bounds: Rect
    notes: list[str]


class BoundaryEvidence(BaseModel):
    landmarks: tuple[str | None, str | None]  # above, below
    backgrounds: tuple[str, str]  # effective paint on each side: colour, "complex", or "canvas"
    background_change: bool | None  # None when either side is complex: unknown, not "same"
    shared_painted_ancestor: str | None  # element painting behind both sides: continuity evidence
    foreground_spans: bool  # content crosses the boundary line: evidence against a split


class BoundaryCandidate(BaseModel):
    id: str
    level: int = Field(ge=0)
    above_region_id: str
    below_region_id: str
    y: float = Field(allow_inf_nan=False)  # document-space boundary line, midway between the two boxes
    box_gap: float = Field(allow_inf_nan=False)  # between the region boxes (negative: boxes overlap)
    content_gap: float | None  # between foreground content extents; None when a side has no content
    evidence: BoundaryEvidence
    notes: list[str]


class Overlap(BaseModel):
    region_ids: tuple[str, str]
    kind: Literal["overlay", "header_over_content", "content"]
    area: float = Field(gt=0, allow_inf_nan=False)


class PageStructure(BaseModel):
    header_group_id: str | None
    footer_group_id: str | None
    main_group_id: str | None
    notes: list[str]  # how each was found, including fallbacks


class CandidateSnapshot(BaseModel):
    schema_version: int = SCHEMA_VERSION
    capture_id: str
    candidates_version: str
    structure: PageStructure
    regions: list[RegionCandidate]
    boundaries: list[BoundaryCandidate]
    overlaps: list[Overlap]

    @model_validator(mode="after")
    def _check(self) -> "CandidateSnapshot":
        ids = {r.id for r in self.regions}
        if len(ids) != len(self.regions):
            raise ValueError("duplicate region IDs")
        refs = [r.parent_region_id for r in self.regions if r.parent_region_id]
        refs += [i for b in self.boundaries for i in (b.above_region_id, b.below_region_id)]
        refs += [i for o in self.overlaps for i in o.region_ids]
        if missing := set(refs) - ids:
            raise ValueError(f"unknown region IDs: {sorted(missing)}")
        return self


class DetectorConfig(BaseModel):
    """Tunable detector parameters (detector.toml). Experimental values, not validated constants."""

    model_config = {"extra": "forbid"}

    gap_ratio_threshold: float = Field(3.0, gt=0, allow_inf_nan=False)
    fallback_spacing_floor: float = Field(16.0, gt=0, allow_inf_nan=False)  # CSS px, when no line height is measurable
    min_spacing_observations: int = Field(2, ge=1)  # internal gaps needed before a side's spacing counts
    # Evaluation baselines: "container" decides on structure and paint only, "gaps" on the gap ratio only.
    mode: Literal["combined", "container", "gaps"] = "combined"


class SpacingEstimate(BaseModel):
    value: float | None  # median positive internal vertical gap; None = unknown (not zero)
    observations: int = Field(ge=0)


class BoundaryProposal(BaseModel):
    id: str  # same ID as the candidate boundary it decides
    level: int = Field(ge=0)
    above_region_id: str
    below_region_id: str
    y: float = Field(allow_inf_nan=False)
    gap: float | None  # g: non-negative content gap; None when a side has no content
    spacing_above: SpacingEstimate  # sA
    spacing_below: SpacingEstimate  # sB
    spacing_floor: float = Field(gt=0, allow_inf_nan=False)
    spacing_floor_source: Literal["line_height", "fallback"]
    spacing_scale: float | None  # max(sA, sB, floor) when a side is known
    gap_ratio: float | None  # g / spacing_scale; None when neither side has reliable spacing
    supporting: list[str]
    opposing: list[str]
    unknowns: list[str]  # evidence that could not be measured: absent, not for or against
    rules: list[str]  # rule outcomes in the order they were checked
    decision: Literal["keep", "split", "ambiguous"]  # keep = same section; split = section boundary
    basis: Literal["landmark", "gap", "visual", "structural", "none"]


class SectionProposal(BaseModel):
    id: str
    order: int = Field(ge=0)
    label: str  # provisional, e.g. "Section 3"; naming is a manual annotation
    role: Literal["header", "footer", "flow", "overlay"]
    region_ids: list[str]
    group_ids: list[str]
    element_ids: list[str]
    bounds: Rect
    boundary_above_id: str | None
    boundary_below_id: str | None
    ambiguous: bool  # an adjoining boundary was undecided; review should look here first
    overlaps: list[tuple[str, Literal["overlay", "header_over_content", "content"]]]
    notes: list[str]


class ProposalSnapshot(BaseModel):
    schema_version: int = SCHEMA_VERSION
    capture_id: str
    detector_version: str
    config: DetectorConfig  # the full effective configuration
    status: Literal["ok", "unsupported"]  # unsupported: no main content found, not "zero sections"
    notes: list[str]
    boundaries: list[BoundaryProposal]
    sections: list[SectionProposal]
    # Groups no section claims: the page root, wrappers opened or split across sections, loose layers.
    unassigned_group_ids: list[str]


class ReviewRegion(BaseModel):
    id: str
    label: str
    role: Literal["header", "footer", "flow", "overlay"]
    status: Literal["proposed", "confirmed", "excluded"]
    bounds: Rect
    element_ids: list[str]
    source_section_ids: list[str]  # proposal sections this region came from
    ambiguous: bool  # came from an undecided proposal boundary
    notes: list[str]  # corrections, manual boundaries and allocation decisions


class ReviewOperation(BaseModel):
    revision: int = Field(ge=1)  # the revision this operation produced
    command: str
    at: datetime


class ReviewState(BaseModel):
    """The user's corrections, kept apart from the automatic proposals (which review never changes)."""

    schema_version: int = SCHEMA_VERSION
    capture_id: str
    proposal_revision: str  # fingerprint of the proposals.json this review started from
    revision: int = Field(ge=0)
    next_region_number: int = Field(ge=1)  # for IDs of regions created by merge/split
    regions: list[ReviewRegion]
    corrections: list[ReviewOperation]
    confirmation_revision: int | None  # revision at which the whole list was confirmed

    @model_validator(mode="after")
    def _check(self) -> "ReviewState":
        ids = [r.id for r in self.regions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate region IDs")
        members = [e for r in self.regions for e in r.element_ids]
        if len(members) != len(set(members)):
            raise ValueError("an element belongs to more than one region")
        return self


class ExportRegion(BaseModel):
    id: str
    order: int = Field(ge=0)
    label: str
    role: Literal["header", "footer", "flow", "overlay"]
    status: Literal["confirmed", "proposed"]  # "proposed" only ever appears in a draft export
    bounds: Rect  # CSS px, document space
    element_ids: list[str]  # snapshot-specific source references (valid for this capture only)
    source_section_ids: list[str]
    overlaps: list[tuple[str, Literal["overlay", "header_over_content", "content"]]]


class ExportDocument(BaseModel):
    schema_version: int = SCHEMA_VERSION
    kind: Literal["sections", "draft-sections"]  # a draft is never mistaken for a confirmed list
    capture_id: str
    page_url: str
    final_url: str | None
    viewport: Viewport
    device_scale_factor: float
    detector_version: str
    proposal_revision: str
    review_revision: int | None  # None: draft straight from the automatic proposals
    regions: list[ExportRegion]

    @model_validator(mode="after")
    def _check(self) -> "ExportDocument":
        if self.kind == "sections" and any(r.status != "confirmed" for r in self.regions):
            raise ValueError("a normal export may only contain confirmed regions")
        ids = [r.id for r in self.regions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate region IDs")
        return self

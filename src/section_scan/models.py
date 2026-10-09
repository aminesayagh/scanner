from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

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
    title: str | None = None  # document title at capture
    device: str | None = None  # emulated Playwright device (user agent, mobile, touch); None = desktop
    description: str | None = None  # meta description (or og:description) at capture
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
    # Defaults keep extractor-1 element files valid; they measured neither clipping nor covering.
    clipped: Literal["none", "partial", "hidden"] = "none"  # own clip-path/clip; "hidden": clipped to nothing
    clipped_ancestor: bool = False  # an ancestor is clipped to nothing, so this element paints nothing
    # An unrelated opaque element is on top at its centre and corners. None: unknown (box not wholly in the
    # viewport at the scroll reference, or the element is skipped by hit testing, e.g. pointer-events: none).
    occluded: bool | None = None
    visible: bool  # all of the above allow it to be seen


class Element(BaseModel):
    id: str
    parent_id: str | None
    child_ids: list[str]
    tag: str
    role: str | None  # explicit role attribute only; landmark tags are read from `tag`
    html_id: str | None = None  # the element's id attribute (scroll anchor); absent before extractor 3
    alt: str | None = None  # an img's alt attribute ("" = decorative); absent before extractor 5
    path: str | None = None  # DOM path anchored on the nearest id, e.g. "#work>ul>li[3]"; absent before extractor 6
    text: str  # whitespace-collapsed own text (not descendants'), truncated
    # Untruncated text: the whole rendered text for h1-h6/p/li, own text otherwise. Absent before extractor 3.
    full_text: str | None = None
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


class DetectorConfig(BaseModel):
    """Tunable analysis parameters (detector.toml). Experimental values, not validated constants.

    Used by all three analysis stages (normalize, candidates, detector). Lengths are CSS px; shares are
    fractions of a width or height in (0, 1].
    """

    model_config = {"extra": "forbid"}

    # Structure (normalize, candidates)
    geometry_tolerance: float = Field(2.0, ge=0, allow_inf_nan=False)  # edges this close count as the same box
    touch_tolerance: float = Field(2.0, ge=0, allow_inf_nan=False)  # overlap this small counts as touching, not crossing
    max_nesting: int = Field(3, ge=1)  # deepest level of nested alternatives below a top-level region
    full_width: float = Field(0.9, gt=0, le=1, allow_inf_nan=False)  # width share that counts as full width
    # Height share a painted (near full-width) descendant must cover to colour its region.
    dominant_paint_height: float = Field(0.5, gt=0, le=1, allow_inf_nan=False)
    # Headers and footers (candidates)
    header_max_top: float = Field(200.0, ge=0, allow_inf_nan=False)  # a header landmark starts within this of the top...
    header_max_height: float = Field(300.0, gt=0, allow_inf_nan=False)  # ...and is at most this tall; else content
    top_edge: float = Field(10.0, ge=0, allow_inf_nan=False)  # a fallback (non-landmark) header starts this close to the top
    footer_max_gap: float = Field(100.0, ge=0, allow_inf_nan=False)  # a footer landmark ends this close to the page bottom...
    footer_min_width: float = Field(0.5, gt=0, le=1, allow_inf_nan=False)  # ...and spans at least this share of the page width
    # Spacing (detector)
    gap_ratio_threshold: float = Field(3.0, gt=0, allow_inf_nan=False)
    fallback_spacing_floor: float = Field(16.0, gt=0, allow_inf_nan=False)  # CSS px, when no line height is measurable
    min_spacing_observations: int = Field(2, ge=1)  # internal gaps needed before a side's spacing counts
    spacing_depth: int = Field(2, ge=1)  # stacking levels of rows that internal spacing is measured in
    # Limitations (detector)
    hidden_min_height: float = Field(300.0, gt=0, allow_inf_nan=False)  # a band of hidden content this tall is flagged
    # Decisions. Evaluation baselines: "container" decides on structure and paint only, "gaps" on the gap ratio only.
    mode: Literal["combined", "container", "gaps"] = "combined"


class GroupSnapshot(BaseModel):
    schema_version: int = SCHEMA_VERSION
    capture_id: str
    normalizer_version: str
    config: DetectorConfig | None = None  # effective configuration; None in files written before it was recorded
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
    config: DetectorConfig | None = None  # effective configuration; None in files written before it was recorded
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


class SectionLimitation(BaseModel):
    """An area inside a section that the capture could not show, so it must not be read as empty."""

    kind: Literal["hidden_content"]  # rendered elements left invisible (e.g. scroll-reveal before scrolling)
    top: float = Field(allow_inf_nan=False)  # document-space y range of the area
    bottom: float = Field(allow_inf_nan=False)
    element_count: int = Field(ge=1)  # hidden elements found in the range


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
    limitations: list[SectionLimitation] = []


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
    limitations: list[SectionLimitation] = []  # carried from the proposals; kept with the part they lie in


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
    limitations: list[SectionLimitation] = []


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


class ReportPosition(BaseModel):
    """Document-space box in CSS px."""

    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class MessageItem(BaseModel):
    model_config = {"extra": "forbid"}

    _eid: str | None = PrivateAttr(None)  # source element (not exported): matches content across breakpoints

    # "a": a link's text; "img": an image's alt text (decorative images, alt="", are left out);
    # "text": any other text
    tag: Literal["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "a", "text", "img"]
    text: str
    hidden: list[str] = []  # breakpoints where this content is not shown (multi-breakpoint reports only)


class MessageListItem(BaseModel):
    """A list item holding headings or paragraphs of its own (e.g. a work entry: date, title, description).

    The only place a third level appears, because the page itself nests these parts in the item.
    """

    model_config = {"extra": "forbid"}

    tag: Literal["li"]
    children: list[MessageItem]
    hidden: list[str] = []  # breakpoints where this content is not shown (multi-breakpoint reports only)
    _eid: str | None = PrivateAttr(None)


class MessageGroup(BaseModel):
    """Items that share a DOM branch: a ul/ol with its items, or a "group".

    One level only: anything nested inside the branch is flattened into `children`, except that a list
    item with headings or paragraphs of its own keeps them as its children.
    """

    model_config = {"extra": "forbid"}

    tag: Literal["group", "ul", "ol"]
    children: list[MessageItem | MessageListItem]
    hidden: list[str] = []  # breakpoints where none of the children is shown (multi-breakpoint reports only)


class ReportSection(BaseModel):
    id: str | None  # id attribute of an element wrapping all of the section's content, for scrolling to it
    position: ReportPosition
    flags: list[str]
    role: Literal["header", "footer", "flow", "overlay"]
    # The section's visible text in page order, following its DOM branches: one entry per branch, a group
    # when the branch holds several items (a lone group is unwrapped). One level deep.
    message: list[MessageItem | MessageListItem | MessageGroup]


class ReportDocument(BaseModel):
    """report.json: a compact summary of a run's sections, top to bottom."""

    link: str
    title: str | None
    description: str | None
    sections: list[ReportSection]


class SiteSection(BaseModel):
    """A section across breakpoints: per-breakpoint position, and the breakpoints where it is not shown."""

    id: str | None
    position: dict[str, ReportPosition]  # breakpoint -> box; missing where the section is hidden
    hidden: list[str]
    flags: list[str]
    role: Literal["header", "footer", "flow", "overlay"]
    message: list[MessageItem | MessageListItem | MessageGroup]


class SiteReportDocument(BaseModel):
    """report.json for a multi-breakpoint capture."""

    link: str
    title: str | None
    description: str | None
    breakpoints: dict[str, Viewport]
    sections: list[SiteSection]

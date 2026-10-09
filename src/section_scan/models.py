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

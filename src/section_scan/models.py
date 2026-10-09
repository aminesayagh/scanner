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
        by_id = {e.id: e for e in self.elements}
        if len(by_id) != len(self.elements):
            raise ValueError("duplicate element IDs")
        for e in self.elements:
            if e.parent_id is not None and e.id not in by_id.get(e.parent_id, e).child_ids:
                raise ValueError(f"{e.id}: parent {e.parent_id} missing or does not list it as a child")
            for c in e.child_ids:
                if c not in by_id or by_id[c].parent_id != e.id:
                    raise ValueError(f"{e.id}: child {c} missing or has another parent")
        return self

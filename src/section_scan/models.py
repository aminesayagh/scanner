from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

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
    readiness: Readiness
    scroll: ScrollPass | None = None
    screenshot: Screenshot | None = None

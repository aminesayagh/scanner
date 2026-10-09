import struct
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from pydantic import ValidationError

from .models import Browser, ElementSnapshot, Manifest, Point, Readiness, Screenshot, ScrollPass, Viewport

# Bump when browser_extract.js changes what it measures, so old element files stay identifiable.
EXTRACTOR_VERSION = "7"
_EXTRACT = (Path(__file__).parent / "browser_extract.js").read_text()


@dataclass(frozen=True)
class Limits:
    """Upper bounds so slow or infinitely loading pages always finish with a recorded outcome."""

    nav_timeout_s: float = 30
    fonts_timeout_s: float = 5
    images_timeout_s: float = 10
    stability_timeout_s: float = 5
    max_scroll_steps: int = 60
    max_scroll_seconds: float = 30
    max_page_height: float = 40_000
    scroll_pause_ms: int = 250
    screenshot_attempts: int = 3


# Cheap layout fingerprint: if page size or element count moves, the layout is still changing.
_LAYOUT_SIGNATURE = """() => {
  const s = document.scrollingElement || document.documentElement;
  return [s.scrollWidth, s.scrollHeight, document.getElementsByTagName('*').length];
}"""

_FONTS_READY = """(ms) => Promise.race([
  document.fonts.ready.then(() => true),
  new Promise(r => setTimeout(() => r(false), ms)),
])"""

_IMAGES_READY = """(ms) => new Promise(resolve => {
  const pending = [...document.images].filter(i => !i.complete);
  let left = pending.length;
  if (!left) return resolve(true);
  const done = () => { if (--left === 0) resolve(true); };
  pending.forEach(i => {
    i.addEventListener('load', done, {once: true});
    i.addEventListener('error', done, {once: true});
  });
  setTimeout(() => resolve(false), ms);
})"""

# Mirrors Playwright's screenshot animations="disabled": finish finite animations, cancel infinite ones,
# so element extraction and the screenshot see the same frozen state.
_FREEZE_ANIMATIONS = """() => document.getAnimations().forEach(a => {
  const end = a.effect && a.effect.getComputedTiming().endTime;
  if (end !== undefined && end !== Infinity) a.finish(); else a.cancel();
})"""

_PAGE_INFO = """() => {
  const meta = document.querySelector('meta[name="description"]') || document.querySelector('meta[property="og:description"]');
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim() || null;
  return [clean(document.title), clean(meta && meta.content)];
}"""

_SCROLL_STATE = """() => {
  const s = document.scrollingElement || document.documentElement;
  return [window.scrollY, window.innerHeight, s.scrollHeight];
}"""


def capture(
    url: str,
    out_dir: Path,
    *,
    width: int,
    height: int,
    scale: float = 1,
    headed: bool = False,
    wait_for_operator: Callable[[], None] | None = None,
    limits: Limits = Limits(),
    device: str | None = None,
) -> Manifest:
    """Capture one URL into out_dir. Browser failures and cancellations are recorded in manifest.json.

    `device` names a Playwright device (e.g. "iPhone 13") whose user agent, mobile mode and touch are used,
    so sites that serve different HTML to phones and tablets do so; width/height still set the viewport.
    """
    if (out_dir / "manifest.json").exists():
        raise FileExistsError(f"{out_dir} already holds a capture; captures are immutable, choose a new --output")

    # Built before mkdir so invalid inputs (e.g. a non-finite scale) fail without leaving a directory.
    manifest = Manifest(
        capture_id=uuid.uuid4().hex,
        status="failed",
        input_url=url,
        captured_at=datetime.now(UTC),
        viewport=Viewport(width=width, height=height),
        device_scale_factor=scale,
        readiness=Readiness(manual=wait_for_operator is not None),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            manifest.browser = Browser(name="chromium", version=browser.version, headless=not headed)
            emulation = {}
            if device:
                d = p.devices[device]
                emulation = {"user_agent": d["user_agent"], "is_mobile": d["is_mobile"], "has_touch": d["has_touch"]}
            context = browser.new_context(
                viewport={"width": width, "height": height}, device_scale_factor=scale, **emulation
            )
            manifest.device = device
            page = context.new_page()
            _run(page, url, out_dir, manifest, wait_for_operator, limits)
            browser.close()
    except PlaywrightError as e:
        manifest.status = "failed"
        manifest.error = str(e)
    except ValidationError as e:
        manifest.status = "failed"
        manifest.error = f"Extracted elements failed validation: {e}"
    except (EOFError, KeyboardInterrupt):
        manifest.status = "failed"
        manifest.error = "Capture cancelled by the operator"

    (out_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2))
    return manifest


def _run(page: Page, url, out_dir, manifest: Manifest, wait_for_operator, limits: Limits) -> None:
    # Track the latest main-frame document response: the operator may navigate (login, challenge) before capture.
    def on_response(response) -> None:
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            manifest.http_status = response.status

    page.on("response", on_response)
    page.goto(url, wait_until="domcontentloaded", timeout=limits.nav_timeout_s * 1000)
    manifest.final_url = page.url

    # In manual mode an error page (e.g. a bot challenge) is the operator's to resolve, so judge it afterwards.
    if wait_for_operator:
        wait_for_operator()
        # The sync API only processes browser events during calls; this one drains what queued while blocked.
        page.wait_for_load_state("domcontentloaded", timeout=limits.nav_timeout_s * 1000)
        manifest.final_url = page.url
    if manifest.http_status is not None and manifest.http_status >= 400:
        manifest.error = f"HTTP {manifest.http_status} from {page.url}"
        return

    r = manifest.readiness
    r.fonts_ready = page.evaluate(_FONTS_READY, limits.fonts_timeout_s * 1000)
    r.layout_stable_before_scroll = _wait_stable(page, limits.stability_timeout_s)

    manifest.scroll = _scroll_pass(page, limits)

    page.evaluate("() => window.scrollTo(0, 0)")
    r.images_ready = page.evaluate(_IMAGES_READY, limits.images_timeout_s * 1000)
    r.layout_stable_before_screenshot = _wait_stable(page, limits.stability_timeout_s)
    x, y = page.evaluate("() => [window.scrollX, window.scrollY]")
    manifest.scroll_reference = Point(x=x, y=y)

    manifest.screenshot, raw = _snapshot(page, out_dir / "screenshot.png", limits.screenshot_attempts)
    snapshot = ElementSnapshot(capture_id=manifest.capture_id, extractor_version=EXTRACTOR_VERSION, **raw)
    (out_dir / "elements.json").write_text(snapshot.model_dump_json(indent=1))
    manifest.extractor_version = EXTRACTOR_VERSION
    manifest.final_url = page.url  # the page may still redirect itself during readiness waits
    manifest.title, manifest.description = page.evaluate(_PAGE_INFO)
    complete = (
        manifest.scroll.truncated_reason is None
        and manifest.screenshot.layout_consistent
        and False not in r.model_dump(exclude={"manual"}).values()  # any timed-out wait
        and len(snapshot.elements) > 0  # an empty page is an unsupported analysis, not a page with no content
    )
    manifest.status = "complete" if complete else "incomplete"


def _wait_stable(page: Page, timeout_s: float, interval_ms: int = 250, samples: int = 3) -> bool:
    """True once the layout signature is identical across `samples` consecutive samples."""
    deadline = time.monotonic() + timeout_s
    last, same = None, 0
    while time.monotonic() < deadline:
        sig = page.evaluate(_LAYOUT_SIGNATURE)
        same = same + 1 if sig == last else 0
        if same >= samples - 1:
            return True
        last = sig
        page.wait_for_timeout(interval_ms)
    return False


def _scroll_pass(page: Page, limits: Limits) -> ScrollPass:
    """Scroll one viewport at a time to trigger lazy loading, stopping at the bottom or a limit."""
    start = time.monotonic()
    steps = 0
    prev_y = None
    while True:
        y, inner, page_height = page.evaluate(_SCROLL_STATE)
        if y + inner >= page_height - 1:
            return ScrollPass(steps=steps, reached_bottom=True, truncated_reason=None, page_height=page_height)
        reason = None
        if y == prev_y:
            # Window did not move although content remains: a nested scroller or a scroll lock.
            reason = "stalled"
        elif page_height > limits.max_page_height:
            reason = "max_height"
        elif steps >= limits.max_scroll_steps:
            reason = "max_steps"
        elif time.monotonic() - start >= limits.max_scroll_seconds:
            reason = "max_seconds"
        if reason:
            return ScrollPass(steps=steps, reached_bottom=False, truncated_reason=reason, page_height=page_height)
        prev_y = y
        page.evaluate("() => window.scrollBy(0, window.innerHeight)")
        steps += 1
        page.wait_for_timeout(limits.scroll_pause_ms)


def _snapshot(page: Page, path: Path, max_attempts: int) -> tuple[Screenshot, dict]:
    """Element extraction plus full-page screenshot as one pair, retried while the layout changes under it."""
    for attempt in range(1, max_attempts + 1):
        page.evaluate(_FREEZE_ANIMATIONS)
        before = page.evaluate(_LAYOUT_SIGNATURE)
        raw = page.evaluate(_EXTRACT)
        png = page.screenshot(path=path, full_page=True, animations="disabled")
        consistent = page.evaluate(_LAYOUT_SIGNATURE) == before
        if consistent:
            break
    width_px, height_px = struct.unpack(">II", png[16:24])  # PNG IHDR width/height
    screenshot = Screenshot(
        path=path.name,
        width_px=width_px,
        height_px=height_px,
        animations_frozen=True,
        attempts=attempt,
        layout_consistent=consistent,
    )
    return screenshot, raw

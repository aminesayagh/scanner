"""Section screenshots and the Excalidraw board of all pages (one column per page, sections top to bottom).

Everything is cropped from the saved screenshots: no site is visited. Cropping uses a canvas in the
Playwright browser already used for capture, so no image library is needed.
"""

import base64
import hashlib
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from .breakpoints import site_runs
from .models import Manifest

DISPLAY_SCALE = 0.5  # board size of a section relative to its CSS size
GAP_X, GAP_Y = 200, 60  # between page columns, between sections in a column
NOTE_SIZE, ROW_NOTE_SIZE = 20, 28

_CROP = """([rects, scale]) => {
  const img = document.getElementById('shot');
  return rects.map(([x, y, w, h]) => {
    x = Math.max(0, Math.round(x)); y = Math.max(0, Math.round(y));
    w = Math.min(Math.round(w), img.naturalWidth - x); h = Math.min(Math.round(h), img.naturalHeight - y);
    if (w < 1 || h < 1) return null;
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(w * scale)); c.height = Math.max(1, Math.round(h * scale));
    c.getContext('2d').drawImage(img, x, y, w, h, 0, 0, c.width, c.height);
    return c.toDataURL('image/png').split(',')[1];
  });
}"""


class Cropper:
    """One browser for many crops of saved screenshots."""

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._page = self._browser.new_page()
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()

    def crop(self, png: bytes, rects: list[tuple[float, float, float, float]], scale: float = 1.0) -> list[bytes | None]:
        """Crops of `png` (rects in screenshot pixels), resized by `scale`; None for an empty crop."""
        data = base64.b64encode(png).decode()
        self._page.set_content(f'<img id="shot" src="data:image/png;base64,{data}">')
        self._page.wait_for_function("document.getElementById('shot').complete")
        return [base64.b64decode(c) if c else None for c in self._page.evaluate(_CROP, [rects, scale])]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "section"


def write_section_shots(run: Path, cropper: Cropper) -> list[Path]:
    """RUN/sections/NN-<id or role>.png: one full-resolution screenshot per section of report.json."""
    manifest = Manifest.model_validate_json((run / "manifest.json").read_text())
    report = json.loads((run / "report.json").read_text())
    dsf = manifest.device_scale_factor
    rects = [(p["x"] * dsf, p["y"] * dsf, p["width"] * dsf, p["height"] * dsf) for p in (s["position"] for s in report["sections"])]
    crops = cropper.crop((run / manifest.screenshot.path).read_bytes(), rects)
    folder = run / "sections"
    if folder.is_dir():
        for old in folder.glob("*.png"):
            old.unlink()
    folder.mkdir(exist_ok=True)
    written = []
    for i, (section, png) in enumerate(zip(report["sections"], crops)):
        if png is None:
            continue
        path = folder / f"{i:02d}-{_slug(section.get('id') or section['role'])}.png"
        path.write_bytes(png)
        written.append(path)
    return written


# --- board ---------------------------------------------------------------------------------------------


def latest_per_page(targets) -> list[Path]:
    """One target per page link: the most recent capture (older folders of the same site are left out)."""
    best: dict[str, tuple] = {}
    for target in targets:
        runs = site_runs(target)
        run = (runs.get("laptop") or next(iter(runs.values()))) if runs else target
        manifest = Manifest.model_validate_json((run / "manifest.json").read_text())
        link = (manifest.final_url or manifest.input_url).rstrip("/")
        if link not in best or manifest.captured_at > best[link][0]:
            best[link] = (manifest.captured_at, target)
    return sorted(t for _, t in best.values())


def _row(target: Path) -> dict:
    """A page's column: its note, screenshot and sections (laptop layout for three-breakpoint captures)."""
    runs = site_runs(target)
    if runs:
        bp = "laptop" if "laptop" in runs else next(iter(runs))
        run = runs[bp]
    else:
        bp, run = None, target
    report_path = target / "report-classified.json"
    report = json.loads((report_path if report_path.exists() else target / "report.json").read_text())
    manifest = Manifest.model_validate_json((run / "manifest.json").read_text())
    sections = []
    for n, s in enumerate(report["sections"], 1):
        pos = s["position"].get(bp) if bp else s["position"]
        if not pos or pos["width"] <= 0 or pos["height"] <= 0:
            continue  # not shown at this breakpoint
        label = (s.get("classification") or {}).get("label") or s["role"]
        note = f"{n}. {label}" + (f"  #{s['id']}" if s.get("id") else "")
        sections.append((note, pos))
    lines = [report.get("title") or "(no title)", report.get("link") or ""]
    page_class = report.get("classification") or {}
    if page_class:
        lines.append(" · ".join(f"{q}: {c.get('label') or '—'}" for q, c in page_class.items()))
    return {
        "note": "\n".join(line for line in lines if line),
        "screenshot": run / manifest.screenshot.path,
        "dsf": manifest.device_scale_factor,
        "sections": sections,
    }


def _base(kind: str, id_: str, x: float, y: float, w: float, h: float) -> dict:
    seed = int(hashlib.sha256(id_.encode()).hexdigest()[:8], 16)
    return {
        "id": id_, "type": kind, "x": x, "y": y, "width": w, "height": h, "angle": 0,
        "strokeColor": "#1e1e1e", "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 1,
        "strokeStyle": "solid", "roughness": 0, "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": None, "seed": seed, "version": 1, "versionNonce": seed, "isDeleted": False,
        "boundElements": None, "updated": 1, "link": None, "locked": False,
    }


def _text(id_: str, x: float, y: float, text: str, size: int) -> dict:
    lines = text.split("\n")
    el = _base("text", id_, x, y, max(len(line) for line in lines) * size * 0.6, len(lines) * size * 1.25)
    el.update({
        "text": text, "originalText": text, "fontSize": size, "fontFamily": 1, "textAlign": "left",
        "verticalAlign": "top", "containerId": None, "autoResize": True, "lineHeight": 1.25,
    })
    return el


def _image(id_: str, x: float, y: float, w: float, h: float, file_id: str) -> dict:
    el = _base("image", id_, x, y, w, h)
    el.update({"strokeColor": "transparent", "status": "saved", "fileId": file_id, "scale": [1, 1], "crop": None})
    return el


def build_board(targets: list[Path], cropper: Cropper) -> dict:
    """An Excalidraw scene: one column per page (left to right), its sections top to bottom, notes top left."""
    elements, files = [], {}
    left = 0.0
    for c, target in enumerate(targets):
        page = _row(target)
        note = _text(f"page{c}-note", left, 0, page["note"], ROW_NOTE_SIZE)
        elements.append(note)
        y = note["height"] + 40
        dsf = page["dsf"]
        rects = [(p["x"] * dsf, p["y"] * dsf, p["width"] * dsf, p["height"] * dsf) for _, p in page["sections"]]
        crops = cropper.crop(page["screenshot"].read_bytes(), rects, DISPLAY_SCALE / dsf)
        widest = note["width"]
        for i, ((label, pos), png) in enumerate(zip(page["sections"], crops)):
            if png is None:
                continue
            file_id = hashlib.sha256(png).hexdigest()[:40]
            files[file_id] = {
                "mimeType": "image/png", "id": file_id, "created": 1, "lastRetrieved": 1,
                "dataURL": "data:image/png;base64," + base64.b64encode(png).decode(),
            }
            w, h = pos["width"] * DISPLAY_SCALE, pos["height"] * DISPLAY_SCALE
            elements.append(_text(f"page{c}-s{i}-note", left, y, label, NOTE_SIZE))
            elements.append(_image(f"page{c}-s{i}", left, y + NOTE_SIZE * 1.25 + 8, w, h, file_id))
            y += NOTE_SIZE * 1.25 + 8 + h + GAP_Y
            widest = max(widest, w)
        left += widest + GAP_X
    return {
        "type": "excalidraw", "version": 2, "source": "section-scan",
        "elements": elements, "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None}, "files": files,
    }

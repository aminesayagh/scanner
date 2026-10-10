"""Section screenshots and the Excalidraw board of all pages (a column per page and breakpoint, sections
top to bottom).

Everything is cropped from the saved screenshots: no site is visited. Cropping uses a canvas in the
Playwright browser already used for capture, so no image library is needed.
"""

import base64
import hashlib
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import review as review_mod
from .breakpoints import site_runs
from .models import ElementSnapshot, Manifest, ProposalSnapshot
from .report import section_rows

DISPLAY_SCALE = 0.5  # board size of a section relative to its CSS size
GAP_X, GAP_BREAKPOINT, GAP_Y = 400, 100, 60  # between pages, between a page's breakpoints, between sections
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


_HIDE = """(css) => {
  document.getElementById('ss-hide')?.remove();
  if (css === null) return;
  const style = document.createElement('style');
  style.id = 'ss-hide';
  style.textContent = css;
  document.documentElement.append(style);
}"""


_FRAME = """(ids) => {
  const rects = ids.map((i) => document.querySelector(`[data-ss-id="${i}"]`)).filter(Boolean)
    .map((n) => n.getBoundingClientRect()).filter((r) => r.width > 0 && r.height > 0);
  if (!rects.length) return null;
  const left = Math.min(...rects.map((r) => r.left)), top = Math.min(...rects.map((r) => r.top));
  const right = Math.max(...rects.map((r) => r.right)), bottom = Math.max(...rects.map((r) => r.bottom));
  return { x: left + scrollX, y: top + scrollY, width: right - left, height: bottom - top };
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

    def isolated(
        self, run: Path, manifest: Manifest, clips: list[dict], plans: list[tuple[list[str], list[str]]]
    ) -> list[bytes | None]:
        """Screenshots of each section from the page's offline copy. plans[i] = (the section's top elements,
        the elements to make transparent first). The section is framed where its own elements are in the
        copy, which can differ from the capture when scripts sized the page (clips[i] is the fallback).

        The copy opens at the captured size and device. Chrome's snapshot keeps no scripts, so the page
        cannot change or randomize itself; hiding by opacity keeps every box where it was.
        """
        emulation = {}
        if manifest.device:
            d = self._pw.devices[manifest.device]
            emulation = {"user_agent": d["user_agent"], "is_mobile": d["is_mobile"], "has_touch": d["has_touch"]}
        context = self._browser.new_context(
            viewport={"width": manifest.viewport.width, "height": manifest.viewport.height},
            # JavaScript stays on for Playwright's own calls; the snapshot holds no page scripts to run.
            device_scale_factor=manifest.device_scale_factor, **emulation,
        )
        page = context.new_page()
        page.goto((run / manifest.snapshot).resolve().as_uri(), wait_until="load")
        shots = []
        for clip, (roots, ids) in zip(clips, plans):
            clip = page.evaluate(_FRAME, roots) or clip
            if clip["width"] < 1 or clip["height"] < 1:
                shots.append(None)
                continue
            rule = ",".join(f'[data-ss-id="{i}"]' for i in ids)
            # Inserted directly: Playwright's add_style_tag waits for a load event an MHTML page never fires.
            page.evaluate(_HIDE, f"{rule} {{ opacity: 0 !important; }}" if ids else "")
            shots.append(page.screenshot(clip=clip, full_page=True, animations="disabled"))
            page.evaluate(_HIDE, None)
        context.close()
        return shots

    def crop(self, png: bytes, rects: list[tuple[float, float, float, float]], scale: float = 1.0) -> list[bytes | None]:
        """Crops of `png` (rects in screenshot pixels), resized by `scale`; None for an empty crop."""
        data = base64.b64encode(png).decode()
        self._page.set_content(f'<img id="shot" src="data:image/png;base64,{data}">')
        self._page.wait_for_function("document.getElementById('shot').complete")
        return [base64.b64decode(c) if c else None for c in self._page.evaluate(_CROP, [rects, scale])]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "section"


def write_section_shots(run: Path, cropper: Cropper) -> list[Path]:
    """RUN/sections/NN-<id or role>.png: one full-resolution screenshot per section of report.json.

    With the page's offline copy (captures since extractor 9), every other section and floating element
    that covers a section is hidden before its screenshot; older captures are cropped from screenshot.png.
    """
    manifest = Manifest.model_validate_json((run / "manifest.json").read_text())
    report = json.loads((run / "report.json").read_text())
    positions = [s["position"] for s in report["sections"]]
    if manifest.snapshot and (run / manifest.snapshot).exists():
        crops = cropper.isolated(run, manifest, positions, _covering(run, len(positions)))
    else:
        dsf = manifest.device_scale_factor
        rects = [(p["x"] * dsf, p["y"] * dsf, p["width"] * dsf, p["height"] * dsf) for p in positions]
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


def _covering(run: Path, count: int) -> list[tuple[list[str], list[str]]]:
    """For each section of report.json (same order): its top elements, and the elements to hide: the top
    elements of other sections that are painted in front of it. What is painted behind it stays (a
    background the section's text is designed to sit on), and so do wrappers that contain it.
    """
    props = ProposalSnapshot.model_validate_json((run / "proposals.json").read_text())
    review = review_mod.load(run)
    if review is not None and review.proposal_revision != review_mod.proposal_revision(run):
        review = None
    rows, members = section_rows(props, review)
    rows = sorted((r for r in rows if r[3] != "excluded"), key=lambda r: r[4].y)
    elements = {e.id: e for e in ElementSnapshot.model_validate_json((run / "elements.json").read_text()).elements}

    def tops(ids):
        own = set(ids)
        return [e for e in ids if elements[e].parent_id not in own]

    def ancestors(eid):
        out = set()
        while (eid := elements[eid].parent_id) is not None:
            out.add(eid)
        return out

    def paint_key(eid):
        # The browser's painting order between two elements, approximately: a higher z-index wins, then a
        # positioned element over a static one, then the later one in the page.
        # ponytail: own z-index only; stacking contexts created by ancestors are not followed.
        style = elements[eid].styles
        z = int(style.get("zIndex")) if style.get("zIndex", "").lstrip("-").isdigit() else 0
        return (z, style.get("position", "static") != "static", int(eid[1:]))

    roots = [tops(members[r[0]]) for r in rows]
    hide = []
    for i in range(min(count, len(rows))):
        around = set().union(*(ancestors(e) for e in roots[i])) if roots[i] else set()
        mine = [paint_key(e) for e in roots[i]]
        hide.append((roots[i], [
            e for j, rs in enumerate(roots) if j != i for e in rs
            if e not in around and any(paint_key(e) > k for k in mine)  # painted in front of this section
        ]))
    return hide + [([], []) for _ in range(count - len(hide))]


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


def _columns(target: Path) -> list[dict]:
    """A page's columns: one per breakpoint (laptop, tablet, mobile), or one for a single-viewport capture.

    Each holds its note, its breakpoint's screenshot and the sections shown at that breakpoint.
    """
    runs = site_runs(target) or {None: target}
    report_path = target / "report-classified.json"
    report = json.loads((report_path if report_path.exists() else target / "report.json").read_text())
    lines = [report.get("title") or "(no title)", report.get("link") or ""]
    page_class = report.get("classification") or {}
    if page_class:
        lines.append(" · ".join(f"{q}: {c.get('label') or '—'}" for q, c in page_class.items()))
    columns = []
    for bp, run in runs.items():
        manifest = Manifest.model_validate_json((run / "manifest.json").read_text())
        sections = []
        for n, s in enumerate(report["sections"], 1):
            pos = s["position"].get(bp) if bp else s["position"]
            if not pos or pos["width"] <= 0 or pos["height"] <= 0:
                continue  # not shown at this breakpoint
            label = (s.get("classification") or {}).get("label") or s["role"]
            sections.append((f"{n}. {label}", pos))
        viewport = f"{bp} · {manifest.viewport.width}×{manifest.viewport.height}" if bp else None
        columns.append({
            "shots": _saved_shots(run),
            "bp": bp or "page",
            "note": "\n".join(line for line in lines + [viewport] if line),
            "screenshot": run / manifest.screenshot.path,
            "dsf": manifest.device_scale_factor,
            "sections": sections,
        })
    return columns


def _key(pos: dict) -> tuple:
    return tuple(round(pos[k]) for k in ("x", "y", "width", "height"))


def _saved_shots(run: Path) -> dict[tuple, Path]:
    """The run's section screenshots (RUN/sections), keyed by their section's position."""
    folder, report_path = run / "sections", run / "report.json"
    if not folder.is_dir() or not report_path.exists():
        return {}
    files = {int(f.name.split("-", 1)[0]): f for f in folder.glob("*.png") if f.name.split("-", 1)[0].isdigit()}
    report = json.loads(report_path.read_text())
    return {_key(s["position"]): files[i] for i, s in enumerate(report["sections"]) if i in files}


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
    """An Excalidraw scene: pages left to right, each as a column per breakpoint (laptop, tablet, mobile)
    with its sections top to bottom; a note at the top left of every column and section.
    """
    elements, files = [], {}
    left = 0.0
    for c, target in enumerate(targets):
        for column in _columns(target):
            prefix = f"page{c}-{column['bp']}"
            note = _text(f"{prefix}-note", left, 0, column["note"], ROW_NOTE_SIZE)
            elements.append(note)
            y = note["height"] + 40
            dsf = column["dsf"]
            crops = []
            for _, p in column["sections"]:
                # The section's own screenshot (covering elements hidden) when saved; else a crop of the page.
                if shot := column["shots"].get(_key(p)):
                    png = shot.read_bytes()
                    size = (round(p["width"] * dsf), round(p["height"] * dsf))
                    crops += cropper.crop(png, [(0, 0, *size)], DISPLAY_SCALE / dsf)
                else:
                    rect = (p["x"] * dsf, p["y"] * dsf, p["width"] * dsf, p["height"] * dsf)
                    crops += cropper.crop(column["screenshot"].read_bytes(), [rect], DISPLAY_SCALE / dsf)
            widest = note["width"]
            for i, ((label, pos), png) in enumerate(zip(column["sections"], crops)):
                if png is None:
                    continue
                file_id = hashlib.sha256(png).hexdigest()[:40]
                files[file_id] = {
                    "mimeType": "image/png", "id": file_id, "created": 1, "lastRetrieved": 1,
                    "dataURL": "data:image/png;base64," + base64.b64encode(png).decode(),
                }
                w, h = pos["width"] * DISPLAY_SCALE, pos["height"] * DISPLAY_SCALE
                elements.append(_text(f"{prefix}-s{i}-note", left, y, label, NOTE_SIZE))
                elements.append(_image(f"{prefix}-s{i}", left, y + NOTE_SIZE * 1.25 + 8, w, h, file_id))
                y += NOTE_SIZE * 1.25 + 8 + h + GAP_Y
                widest = max(widest, w)
            left += widest + GAP_BREAKPOINT
        left += GAP_X - GAP_BREAKPOINT
    return {
        "type": "excalidraw", "version": 2, "source": "section-scan",
        "elements": elements, "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None}, "files": files,
    }

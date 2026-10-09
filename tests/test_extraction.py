import base64
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from typer.testing import CliRunner

from section_scan.capture import capture
from section_scan.cli import app
from section_scan.models import ElementSnapshot

FIXTURES = Path(__file__).parent.parent / "fixtures"
VIEWPORT_H = 600
SCALE = 2


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("cap") / "run"
    m = capture((FIXTURES / "extraction.html").resolve().as_uri(), out, width=800, height=VIEWPORT_H, scale=SCALE)
    assert m.status == "complete", m.error
    assert m.extractor_version
    snap = ElementSnapshot.model_validate_json((out / "elements.json").read_text())
    by_text = {e.text: e for e in snap.elements if e.text}
    by_id = {e.id: e for e in snap.elements}
    return out, snap, by_text, by_id


def sample_pixels(png: Path, points: list[tuple[float, float]]) -> list[list[int]]:
    """Read RGB at screenshot-pixel coordinates by decoding the PNG in a browser canvas."""
    data = base64.b64encode(png.read_bytes()).decode()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(f'<img id="i" src="data:image/png;base64,{data}">')
        page.wait_for_function("document.getElementById('i').complete")
        result = page.evaluate(
            """(pts) => {
                const i = document.getElementById('i'), c = document.createElement('canvas');
                c.width = i.naturalWidth; c.height = i.naturalHeight;
                const g = c.getContext('2d'); g.drawImage(i, 0, 0);
                return pts.map(([x, y]) => [...g.getImageData(x, y, 1, 1).data.slice(0, 3)]);
            }""",
            points,
        )
        browser.close()
    return result


def rgb(css: str) -> list[int]:
    return [int(v) for v in css.removeprefix("rgb(").removesuffix(")").split(",")]


def test_rectangles_align_with_screenshot(run):
    out, _, by_text, by_id = run
    blocks = [
        by_id[by_text["Logo"].parent_id],  # fixed header
        by_id[by_text["Hero title"].parent_id],  # hero section
        by_text["Part A"],  # inside a display: contents wrapper
        by_text["Part B"],
        by_id[by_text["Below the fold"].parent_id],  # far below the first viewport
    ]
    centers = [((e.bounds.x + e.bounds.width / 2) * SCALE, (e.bounds.y + e.bounds.height / 2) * SCALE) for e in blocks]
    for e, got in zip(blocks, sample_pixels(out / "screenshot.png", centers)):
        assert got == rgb(e.styles["backgroundColor"]), e


def test_below_the_fold_content_is_present(run):
    _, _, by_text, _ = run
    assert by_text["Below the fold"].bounds.y > VIEWPORT_H


def test_hierarchy_is_preserved(run):
    _, snap, by_text, by_id = run
    body = snap.elements[0]
    assert body.tag == "body" and body.parent_id is None
    wrap = by_id[by_text["Part A"].parent_id]
    assert wrap.box == "contents" and wrap.parent_id == body.id
    assert [by_id[c].text for c in wrap.child_ids] == ["Part A", "Part B"]


def test_contents_wrapper_bounds_cover_its_children(run):
    _, _, by_text, by_id = run
    a, b = by_text["Part A"], by_text["Part B"]
    wrap = by_id[a.parent_id]
    assert wrap.bounds.y == a.bounds.y
    assert wrap.bounds.height == pytest.approx(a.bounds.height + b.bounds.height)


def test_hidden_and_offscreen_are_handled_differently(run):
    _, _, by_text, _ = run
    assert "Mobile menu" not in by_text  # display: none is not rendered, so not extracted
    ghost = by_text["Ghost text"].visibility
    assert ghost.css_visibility == "hidden" and ghost.has_box and not ghost.visible
    sr = by_text["Screen reader only"].visibility
    assert sr.offscreen and not sr.visible
    faded = by_text["Faded child"].visibility
    assert faded.transparent_ancestor and not faded.visible
    assert by_text["Hero title"].visibility.visible


def test_positioning_metadata(run):
    _, _, by_text, by_id = run
    assert by_id[by_text["Logo"].parent_id].positioning == "fixed"
    assert by_text["Logo"].positioning == "fixed"  # inherited from the fixed header
    assert by_text["Sticky bar"].positioning == "sticky"
    assert by_text["Hero title"].positioning == "flow"


def test_uninspectable_areas_are_recorded(run):
    _, snap, by_text, by_id = run
    limits = {lim for e in snap.elements for lim in e.limitations}
    assert {"iframe", "canvas"} <= limits
    assert "scroll_container" in by_id[by_text["Scrolled content"].parent_id].limitations


def test_overlay_draws_selected_elements(run):
    out, _, by_text, _ = run
    hero_h1 = by_text["Hero title"]
    result = CliRunner().invoke(app, ["overlay", str(out), "--id", hero_h1.id])
    assert result.exit_code == 0, result.output
    html = (out / "overlay.html").read_text()
    assert f"top:{hero_h1.bounds.y}px" in html
    assert '<div class="page" style="width:800.0px">' in html  # 1600 screenshot px / scale 2


def test_overlay_rejects_unknown_ids(run):
    out, *_ = run
    result = CliRunner().invoke(app, ["overlay", str(out), "--id", "nope"])
    assert result.exit_code == 2
    assert "Unknown element IDs: nope" in result.output


def test_snapshot_rejects_broken_references():
    el = dict(
        tag="div", role=None, text="", box="normal", positioning="flow", styles={}, limitations=[],
        bounds=dict(x=0, y=0, width=1, height=1), viewport_bounds=dict(x=0, y=0, width=1, height=1),
        visibility=dict(css_visibility="visible", opacity=1, transparent_ancestor=False,
                        has_box=True, offscreen=False, visible=True),
    )
    base = dict(capture_id="c", extractor_version="1", scroll=dict(x=0, y=0),
                document=dict(x=0, y=0, width=1, height=1))
    orphan = [dict(el, id="e0", parent_id=None, child_ids=["e1"]), dict(el, id="e1", parent_id="e9", child_ids=[])]
    with pytest.raises(ValueError, match="e1"):
        ElementSnapshot(**base, elements=orphan)
    with pytest.raises(ValueError, match="duplicate"):
        ElementSnapshot(**base, elements=[dict(el, id="e0", parent_id=None, child_ids=[])] * 2)

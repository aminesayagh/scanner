import base64
import json
import shutil
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from typer.testing import CliRunner

from section_scan.cli import app

FIXTURES = Path(__file__).parent.parent / "fixtures"


def cli(*args):
    return CliRunner().invoke(app, [str(a) for a in args])


def pixel(png: bytes, x: int, y: int) -> list[int]:
    data = base64.b64encode(png).decode()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(f'<img id="i" src="data:image/png;base64,{data}">')
        page.wait_for_function("document.getElementById('i').complete")
        rgb = page.evaluate("""([x, y]) => { const i = document.getElementById('i'), c = document.createElement('canvas');
            c.width = i.naturalWidth; c.height = i.naturalHeight; const g = c.getContext('2d'); g.drawImage(i, 0, 0);
            return [...g.getImageData(x, y, 1, 1).data.slice(0, 3)]; }""", [x, y])
        browser.close()
    return rgb


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("board") / "runs"
    single, site = root / "landmarks", root / "responsive"
    assert cli("capture", (FIXTURES / "candidates-landmarks.html").resolve().as_uri(), "-o", single, "--width", "1000", "--height", "700").exit_code == 0
    assert cli("capture", (FIXTURES / "responsive.html").resolve().as_uri(), "-o", site).exit_code == 0
    for target in (single, site):
        assert cli("detect", target).exit_code == 0
        assert cli("report", target).exit_code == 0
    return root, single, site


def test_report_saves_one_screenshot_per_section(runs):
    _, single, site = runs
    report = json.loads((single / "report.json").read_text())
    shots = sorted((single / "sections").glob("*.png"))
    assert len(shots) == len(report["sections"])
    hero_index = next(i for i, s in enumerate(report["sections"]) if "Hero title" in json.dumps(s["message"]))
    hero = next(p for p in shots if p.name.startswith(f"{hero_index:02d}-"))
    assert pixel(hero.read_bytes(), 5, 60) == [20, 20, 40]  # the hero's own background, not a neighbour's
    assert all((site / bp / "sections").is_dir() for bp in ("laptop", "tablet", "mobile"))


def test_board_has_a_row_per_page_and_sections_left_to_right(runs, tmp_path):
    root, single, site = runs
    out = tmp_path / "board.excalidraw"
    result = cli("board", single, site, "-o", out)
    assert result.exit_code == 0, result.output
    scene = json.loads(out.read_text())
    assert scene["type"] == "excalidraw" and scene["version"] == 2
    elements = {e["id"]: e for e in scene["elements"]}
    notes = [e for e in scene["elements"] if e["id"].endswith("-note") and "-s" not in e["id"]]
    assert [n["id"] for n in notes] == ["row0-note", "row1-note"] and notes[0]["y"] < notes[1]["y"]
    assert "Candidates (landmarks)" in notes[0]["text"] and "Responsive" in notes[1]["text"]
    for r in (0, 1):
        images = [e for e in scene["elements"] if e["type"] == "image" and e["id"].startswith(f"row{r}-")]
        xs = [i["x"] for i in images]
        assert images and all(a < b for a, b in zip(xs, xs[1:]))  # strictly left to right
        assert len({i["y"] for i in images}) == 1 and images[0]["y"] > notes[r]["y"]  # one row, below its note
        for image in images:
            note = elements[image["id"] + "-note"]
            assert note["x"] == image["x"] and note["y"] < image["y"]  # note at the section's top left
            assert scene["files"][image["fileId"]]["dataURL"].startswith("data:image/png;base64,")
    row1 = [e for e in scene["elements"] if e["id"].startswith("row1-s") and e["type"] == "text"]
    assert not any("#app" in e["text"] for e in row1)  # the mobile-only section is not in the laptop row
    assert all(e["y"] > max(i["y"] + i["height"] for i in scene["elements"] if i["id"].startswith("row0-s") and i["type"] == "image")
               for e in scene["elements"] if e["id"].startswith("row1-"))  # rows do not overlap


def test_board_uses_classified_labels_and_page_classification(runs, tmp_path):
    _, single, _ = runs
    copy = tmp_path / "classified"
    shutil.copytree(single, copy)
    report = json.loads((copy / "report.json").read_text())
    for s in report["sections"]:
        s["classification"] = {"label": "hero" if "Hero title" in json.dumps(s["message"]) else None, "confidence": None}
    report["classification"] = {"industry": {"label": "software_it", "confidence": 1.0}, "scope": {"label": "company_site", "confidence": 0.8}}
    (copy / "report-classified.json").write_text(json.dumps(report))
    out = tmp_path / "board.excalidraw"
    assert cli("board", copy, "-o", out).exit_code == 0
    texts = [e["text"] for e in json.loads(out.read_text())["elements"] if e["type"] == "text"]
    assert any(t.endswith(". hero") or ". hero  #" in t for t in texts)
    assert "industry: software_it · scope: company_site" in texts[0]


def test_board_default_collects_each_page_once_from_runs(runs, tmp_path, monkeypatch):
    root, single, _ = runs
    older = root / "landmarks-old"
    shutil.copytree(single, older)
    manifest = json.loads((older / "manifest.json").read_text())
    manifest["captured_at"] = "2020-01-01T00:00:00Z"  # an earlier capture of the same page
    (older / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.chdir(root.parent)
    result = cli("board")
    assert result.exit_code == 0 and "2 pages" in result.output  # the older folder is left out
    assert (root.parent / "boards" / "sections.excalidraw").exists()

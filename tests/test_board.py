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


def test_board_has_a_column_per_page_and_breakpoint_with_sections_top_to_bottom(runs, tmp_path):
    root, single, site = runs
    out = tmp_path / "board.excalidraw"
    result = cli("board", single, site, "-o", out)
    assert result.exit_code == 0, result.output
    scene = json.loads(out.read_text())
    assert scene["type"] == "excalidraw" and scene["version"] == 2
    elements = {e["id"]: e for e in scene["elements"]}
    notes = [e for e in scene["elements"] if e["id"].endswith("-note") and "-s" not in e["id"]]
    # The single capture has one column; the three-breakpoint page has laptop, tablet, mobile, left to right.
    assert [n["id"] for n in notes] == ["page0-page-note", "page1-laptop-note", "page1-tablet-note", "page1-mobile-note"]
    xs = [n["x"] for n in notes]
    assert all(a < b for a, b in zip(xs, xs[1:])) and len({n["y"] for n in notes}) == 1
    assert "Candidates (landmarks)" in notes[0]["text"] and "Responsive" in notes[1]["text"]
    assert [n["text"].splitlines()[-1] for n in notes[1:]] == ["laptop · 1440×900", "tablet · 768×1024", "mobile · 390×844"]
    columns = {n["id"].removesuffix("-note"): n for n in notes}
    for prefix, note in columns.items():
        images = [e for e in scene["elements"] if e["type"] == "image" and e["id"].startswith(prefix + "-s")]
        ys = [i["y"] for i in images]
        assert images and all(a < b for a, b in zip(ys, ys[1:]))  # strictly top to bottom
        assert len({i["x"] for i in images}) == 1 and images[0]["x"] == note["x"]  # one column, under its note
        assert all(a["y"] + a["height"] < b["y"] for a, b in zip(images, images[1:]))  # sections do not overlap
        for image in images:
            label = elements[image["id"] + "-note"]
            assert label["x"] == image["x"] and label["y"] < image["y"]  # note at the section's top left
            assert scene["files"][image["fileId"]]["dataURL"].startswith("data:image/png;base64,")
    order = {s["id"]: n for n, s in enumerate(json.loads((site / "report.json").read_text())["sections"], 1)}

    def numbers(prefix):  # section numbers shown in a column ("3. flow" -> 3)
        return {int(e["text"].split(".")[0]) for e in scene["elements"] if e["id"].startswith(prefix + "-s") and e["type"] == "text"}

    assert order["app"] in numbers("page1-mobile") and order["app"] not in numbers("page1-laptop")  # mobile-only
    assert order["gallery"] in numbers("page1-laptop") and order["gallery"] not in numbers("page1-mobile")  # hidden on mobile
    labels = [e["text"] for e in scene["elements"] if "-s" in e["id"] and e["type"] == "text"]
    assert labels and not any("#" in t for t in labels)  # notes carry the order and label only, no DOM id
    ordered = list(columns)
    for left_col, right_col in zip(ordered, ordered[1:]):  # columns do not overlap
        right_edge = max(e["x"] + e["width"] for e in scene["elements"] if e["id"].startswith(left_col + "-"))
        assert all(e["x"] > right_edge for e in scene["elements"] if e["id"].startswith(right_col + "-"))


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
    assert any(t.endswith(". hero") for t in texts)
    assert "industry: software_it · scope: company_site" in texts[0]  # the page note comes first


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


def test_each_breakpoint_column_is_cropped_from_its_own_screenshot(runs, tmp_path):
    _, _, site = runs
    out = tmp_path / "board.excalidraw"
    assert cli("board", site, "-o", out).exit_code == 0
    scene = json.loads(out.read_text())
    n = next(n for n, s in enumerate(json.loads((site / "report.json").read_text())["sections"], 1) if s["id"] == "app")
    label = next(e for e in scene["elements"] if e["id"].startswith("page0-mobile-s") and e.get("text", "").startswith(f"{n}. "))
    image = next(e for e in scene["elements"] if e["id"] == label["id"].removesuffix("-note"))
    png = base64.b64decode(scene["files"][image["fileId"]]["dataURL"].split(",", 1)[1])
    assert pixel(png, 3, 3) == [230, 230, 255]  # the mobile-only section's own background



@pytest.fixture(scope="module")
def covered(tmp_path_factory):
    out = tmp_path_factory.mktemp("covered") / "run"
    url = (FIXTURES / "covered.html").resolve().as_uri()
    assert cli("capture", url, "-o", out, "--width", "1000", "--height", "700").exit_code == 0
    assert cli("detect", out).exit_code == 0 and cli("report", out).exit_code == 0
    report = json.loads((out / "report.json").read_text())
    index = next(i for i, s in enumerate(report["sections"]) if "First section" in json.dumps(s["message"]))
    assert report["sections"][index]["position"]["y"] == 0 and report["sections"][index]["position"]["height"] == 480
    return out, index


def first_shot(run, index):
    return next((run / "sections").glob(f"{index:02d}-*.png")).read_bytes()


def test_section_screenshot_hides_sections_and_floating_elements_covering_it(covered):
    run, index = covered
    png = first_shot(run, index)
    assert pixel(png, 10, 340) == [200, 30, 30]  # under the fixed cookie bar (green in the page screenshot)
    assert pixel(png, 10, 440) == [200, 30, 30]  # under the section pulled up over it (blue in the page screenshot)
    page = (run / "screenshot.png").read_bytes()
    assert pixel(page, 10, 340) == [30, 160, 60] and pixel(page, 10, 440) == [30, 30, 200]  # what was covering it


def test_board_uses_the_clean_section_screenshot(covered, tmp_path):
    run, index = covered
    out = tmp_path / "board.excalidraw"
    assert cli("board", run, "-o", out).exit_code == 0
    scene = json.loads(out.read_text())
    image = next(e for e in scene["elements"] if e["id"] == f"page0-page-s{index}")
    png = base64.b64decode(scene["files"][image["fileId"]]["dataURL"].split(",", 1)[1])
    assert pixel(png, 5, 220) == [200, 30, 30]  # half size: y 440 of the section, still the section's own red


def test_captures_without_an_offline_copy_fall_back_to_cropping(covered, tmp_path):
    run, index = covered
    old = tmp_path / "old"
    shutil.copytree(run, old)
    (old / "page.mhtml").unlink()
    manifest = json.loads((old / "manifest.json").read_text())
    manifest["snapshot"] = None  # as captured before extractor 9
    (old / "manifest.json").write_text(json.dumps(manifest))
    assert cli("report", old).exit_code == 0
    assert pixel(first_shot(old, index), 10, 440) == [30, 30, 200]  # a plain crop keeps what covered it



def test_what_is_painted_behind_a_section_stays_in_its_screenshot(covered):
    run, _ = covered
    report = json.loads((run / "report.json").read_text())
    over = next(i for i, s in enumerate(report["sections"]) if "Over section" in json.dumps(s["message"]))
    base = next(i for i, s in enumerate(report["sections"]) if "Base section" in json.dumps(s["message"]))
    assert over != base
    # The base is painted behind the transparent section: it stays, so the white text keeps its background.
    assert pixel(first_shot(run, over), 300, 280) == [20, 30, 90]



def test_section_is_framed_where_the_offline_copy_lays_it_out(tmp_path):
    # A script makes the page taller after load (as pinned-scroll libraries do); the copy keeps no scripts,
    # so everything below sits higher in the copy than in the capture. The shot must still frame the section.
    page = tmp_path / "scripted.html"
    page.write_text("""<!doctype html><html><head><meta charset="utf-8"><title>Scripted</title></head>
      <body style="margin: 0"><div id="spacer"></div>
      <section style="height: 300px; padding: 20px; background: rgb(20, 120, 20)"><h2>Green</h2></section>
      <section style="height: 300px; padding: 20px; background: rgb(120, 20, 120)"><h2>Purple</h2></section>
      <script>
        // A constructed stylesheet, as some libraries and web components use: not part of the DOM, so the
        // snapshot does not keep it and the copy lays the page out without it.
        const sheet = new CSSStyleSheet();
        sheet.replaceSync('#spacer { height: 500px; }');
        document.adoptedStyleSheets = [sheet];
      </script>
      </body></html>""")
    out = tmp_path / "run"
    assert cli("capture", page.as_uri(), "-o", out, "--width", "1000", "--height", "700").exit_code == 0
    assert cli("detect", out).exit_code == 0 and cli("report", out).exit_code == 0
    report = json.loads((out / "report.json").read_text())
    green = next(i for i, s in enumerate(report["sections"]) if "Green" in json.dumps(s["message"]))
    assert report["sections"][green]["position"]["y"] == 500  # pushed down by the script at capture
    assert pixel(first_shot(out, green), 10, 10) == [20, 120, 20]
    assert pixel(first_shot(out, green), 10, 330) == [20, 120, 20]  # its bottom edge, not the purple below

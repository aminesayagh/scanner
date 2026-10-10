import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from section_scan.cli import app
from section_scan.models import Manifest, SiteReportDocument

FIXTURES = Path(__file__).parent.parent / "fixtures"


def cli(*args):
    return CliRunner().invoke(app, [str(a) for a in args])


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site") / "responsive"
    url = (FIXTURES / "responsive.html").resolve().as_uri()
    assert cli("capture", url, "-o", out).exit_code == 0
    assert cli("detect", out).exit_code == 0
    result = cli("report", out)
    assert result.exit_code == 0, result.output
    doc = json.loads((out / "report.json").read_text())
    SiteReportDocument.model_validate(doc)
    return out, doc


def by_id(doc, section_id):
    return next(s for s in doc["sections"] if s["id"] == section_id)


def items(section):
    out = []
    for e in section["message"]:
        out += e.get("children", [e]) if e.get("tag") == "group" else [e]
    return out


def test_each_breakpoint_is_a_separate_device_load(site):
    out, doc = site
    devices = {bp: Manifest.model_validate_json((out / bp / "manifest.json").read_text()) for bp in ("laptop", "tablet", "mobile")}
    assert {bp: (m.viewport.width, m.device) for bp, m in devices.items()} == {
        "laptop": (1440, None), "tablet": (768, "iPad Mini"), "mobile": (390, "iPhone 13")}
    assert len({m.capture_id for m in devices.values()}) == 3
    assert set(doc["breakpoints"]) == {"laptop", "tablet", "mobile"}
    assert doc["title"] == "Responsive" and doc["description"] == "Breakpoint cases."


def test_sections_hidden_per_breakpoint(site):
    _, doc = site
    assert by_id(doc, "intro")["hidden"] == [] and set(by_id(doc, "intro")["position"]) == {"laptop", "tablet", "mobile"}
    gallery = by_id(doc, "gallery")
    assert gallery["hidden"] == ["mobile"] and set(gallery["position"]) == {"laptop", "tablet"}
    app_section = by_id(doc, "app")
    assert app_section["hidden"] == ["laptop", "tablet"] and set(app_section["position"]) == {"mobile"}
    ids = [s["id"] for s in doc["sections"]]
    assert ids.index("intro") < ids.index("app") < ids.index("contact")  # mobile-only section in its place


def test_content_hidden_per_breakpoint(site):
    _, doc = site
    intro = {i.get("text", ""): i for i in items(by_id(doc, "intro"))}
    assert intro["Long desktop paragraph"]["hidden"] == ["mobile"]
    assert "hidden" not in intro["Shown everywhere"]
    header = {i.get("text", ""): i for s in doc["sections"] if s["role"] == "header" for i in items(s)}
    assert header["Menu"]["hidden"] == ["laptop", "tablet"]
    assert header["Home"]["hidden"] == ["mobile"]


def test_random_content_is_listed_once_not_hidden(site):
    _, doc = site
    quotes = [i for s in doc["sections"] for i in items(s) if i.get("text", "").startswith("Quote ")]
    assert len(quotes) == 1 and "hidden" not in quotes[0]  # three different texts, one slot


def test_single_viewport_capture_still_works(tmp_path):
    out = tmp_path / "single"
    url = (FIXTURES / "responsive.html").resolve().as_uri()
    assert cli("capture", url, "-o", out, "--width", "390", "--height", "844").exit_code == 0
    assert (out / "manifest.json").exists() and not (out / "mobile").exists()


def test_same_text_in_different_elements_is_one_content(site):
    _, doc = site
    header = [i for s in doc["sections"] if s["role"] == "header" for i in items(s)]
    contact = [i for i in header if i.get("text", "") == "Contact us"]
    assert len(contact) == 1 and "hidden" not in contact[0]  # desktop nav link and mobile menu link


def test_section_without_content_is_matched_not_duplicated(site):
    _, doc = site
    bands = [s for s in doc["sections"] if s["id"] == "band" or (not s["message"] and s["role"] == "flow")]
    assert len(bands) == 1 and bands[0]["hidden"] == []


def test_phone_user_agent_content_is_captured(site):
    _, doc = site
    intro = {i.get("text", ""): i for i in items(by_id(doc, "intro"))}
    assert intro["Open in the app"]["hidden"] == ["laptop", "tablet"]  # served only to the emulated iPhone


def test_shuffled_gallery_rebuilt_per_width_lists_each_image_once(site):
    _, doc = site
    shots = [i for i in items(by_id(doc, "shots")) if i["tag"] == "img"]
    assert sorted(i.get("text", "") for i in shots) == [f"Shot {n}" for n in range(1, 7)]
    assert not any("hidden" in i for i in shots)


def test_image_without_alt_is_matched_by_source(site):
    _, doc = site
    logos = [i for s in doc["sections"] for i in items(s) if i.get("src", "").endswith("logo-mark.svg")]
    assert len(logos) == 1 and "text" not in logos[0] and "hidden" not in logos[0]
    # One alt-less image only on desktop, another only on mobile: only their sources tell them apart.
    badges = {i["src"].rsplit("/", 1)[-1]: i.get("hidden") for s in doc["sections"] for i in items(s) if "badge-" in i.get("src", "")}
    assert badges == {"badge-a.svg": ["mobile"], "badge-b.svg": ["laptop", "tablet"]}


def test_three_breakpoint_capture_replaces_an_older_single_capture(tmp_path):
    out = tmp_path / "page"
    url = (FIXTURES / "responsive.html").resolve().as_uri()
    assert cli("capture", url, "-o", out, "--width", "800", "--height", "600").exit_code == 0
    assert (out / "manifest.json").exists()
    assert cli("capture", url, "-o", out).exit_code == 0
    assert not (out / "manifest.json").exists() and (out / "laptop" / "manifest.json").exists()

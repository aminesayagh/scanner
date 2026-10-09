import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from section_scan.capture import Limits, capture
from section_scan.cli import app

FIXTURES = Path(__file__).parent.parent / "fixtures"


def fixture_url(name: str) -> str:
    return (FIXTURES / name).resolve().as_uri()


def test_capture_writes_manifest_and_screenshot_at_viewport(tmp_path):
    m = capture(fixture_url("basic.html"), tmp_path / "run", width=800, height=600, scale=2)
    assert m.status == "complete", m.error
    saved = json.loads((tmp_path / "run" / "manifest.json").read_text())
    assert saved["capture_id"] == m.capture_id
    assert saved["viewport"] == {"width": 800, "height": 600}
    assert (tmp_path / "run" / "screenshot.png").exists()
    # Full page at scale 2: screenshot pixels are twice the CSS viewport width.
    assert m.screenshot.width_px == 1600
    assert m.screenshot.height_px > 600 * 2
    assert m.scroll_reference.y == 0


def test_lazy_content_is_loaded_before_screenshot(tmp_path):
    m = capture(fixture_url("lazy.html"), tmp_path / "run", width=800, height=600)
    assert m.status == "complete", m.error
    assert m.scroll.steps > 0
    # The lazy block (800px) only exists if the scroll pass revealed it before the screenshot.
    assert m.screenshot.height_px >= 2500 + 800


def test_infinite_scroll_reaches_bounded_recorded_outcome(tmp_path):
    limits = Limits(max_scroll_steps=5, scroll_pause_ms=50, stability_timeout_s=1)
    m = capture(fixture_url("infinite-scroll.html"), tmp_path / "run", width=800, height=600, limits=limits)
    assert m.status == "incomplete"
    assert m.scroll.truncated_reason == "max_steps"
    assert not m.scroll.reached_bottom


def test_navigation_failure_is_recorded_and_exits_nonzero(tmp_path):
    out = tmp_path / "run"
    result = CliRunner().invoke(app, ["capture", "http://127.0.0.1:9/", "--output", str(out)])
    assert result.exit_code == 1
    assert "Capture failed" in result.output
    assert json.loads((out / "manifest.json").read_text())["status"] == "failed"


def test_existing_capture_is_never_overwritten(tmp_path):
    out = tmp_path / "run"
    capture(fixture_url("basic.html"), out, width=800, height=600)
    before = (out / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        capture(fixture_url("basic.html"), out, width=800, height=600)
    assert (out / "manifest.json").read_bytes() == before


def test_manual_ready_requires_headed(tmp_path):
    result = CliRunner().invoke(app, ["capture", "https://example.com", "-o", str(tmp_path / "r"), "--manual-ready"])
    assert result.exit_code == 2
    assert not (tmp_path / "r").exists()


def test_cli_success_reports_no_false_timeouts(tmp_path):
    result = CliRunner().invoke(app, ["capture", fixture_url("basic.html"), "-o", str(tmp_path / "r")])
    assert result.exit_code == 0, result.output
    assert "Capture complete" in result.output
    assert "timed out" not in result.output

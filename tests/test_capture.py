import json
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from threading import Thread
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
    assert "capture failed" in result.output
    # Without --width/--height all three breakpoints are attempted, each recording its failure.
    assert all(json.loads((out / bp / "manifest.json").read_text())["status"] == "failed" for bp in ("laptop", "tablet", "mobile"))


def test_recapture_updates_the_folder_and_keeps_self_checking_state(tmp_path):
    out = tmp_path / "run"
    first = capture(fixture_url("basic.html"), out, width=800, height=600)
    (out / "proposals.json").write_text("{}")  # derived from the old capture
    (out / "sections").mkdir()
    (out / "sections" / "00-header.png").write_bytes(b"old")
    (out / "classification.json").write_text("{}")  # checks itself against new input
    second = capture(fixture_url("basic.html"), out, width=800, height=600)
    assert second.capture_id != first.capture_id
    assert json.loads((out / "manifest.json").read_text())["capture_id"] == second.capture_id
    assert not (out / "proposals.json").exists() and not (out / "sections").exists()
    assert (out / "classification.json").exists()


def test_manual_ready_requires_headed(tmp_path):
    result = CliRunner().invoke(app, ["capture", "https://example.com", "-o", str(tmp_path / "r"), "--manual-ready"])
    assert result.exit_code == 2
    assert not (tmp_path / "r").exists()


def test_cli_success_reports_no_false_timeouts(tmp_path):
    result = CliRunner().invoke(app, ["capture", fixture_url("basic.html"), "-o", str(tmp_path / "r")])
    assert result.exit_code == 0, result.output
    assert result.output.count("capture complete") == 3  # laptop, tablet, mobile
    assert "timed out" not in result.output


def test_manual_mode_records_the_page_actually_captured(tmp_path):
    def operator():
        time.sleep(1)  # the page navigates itself to basic.html meanwhile

    m = capture(fixture_url("redirect-later.html"), tmp_path / "r", width=800, height=600, wait_for_operator=operator)
    assert m.status == "complete", m.error
    assert m.final_url.endswith("/basic.html")


@pytest.fixture
def http_404_url():
    server = HTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)  # serves cwd; the path below does not exist
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/missing-page"
    server.shutdown()


def test_http_error_fails_the_capture(tmp_path, http_404_url):
    m = capture(http_404_url, tmp_path / "r", width=800, height=600)
    assert m.status == "failed"
    assert m.http_status == 404


def test_manual_mode_prompts_before_judging_an_error_page(tmp_path, http_404_url):
    prompted = []
    m = capture(http_404_url, tmp_path / "r", width=800, height=600, wait_for_operator=lambda: prompted.append(1))
    assert prompted, "operator must get the chance to resolve an error page"
    assert m.status == "failed" and m.http_status == 404


def test_cancel_at_prompt_records_failed_manifest(tmp_path):
    def operator():
        raise EOFError

    m = capture(fixture_url("basic.html"), tmp_path / "r", width=800, height=600, wait_for_operator=operator)
    assert m.status == "failed"
    assert json.loads((tmp_path / "r" / "manifest.json").read_text())["error"] == "Capture cancelled by the operator"


@pytest.mark.parametrize("scale", ["inf", "nan"])
def test_non_finite_scale_is_rejected_without_a_run_dir(tmp_path, scale):
    out = tmp_path / "r"
    result = CliRunner().invoke(app, ["capture", "https://example.com", "-o", str(out), "--scale", scale])
    assert result.exit_code == 2
    assert not out.exists()

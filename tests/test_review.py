import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from section_scan.capture import capture
from section_scan.cli import app
from section_scan.models import ElementSnapshot, ReviewState

FIXTURES = Path(__file__).parent.parent / "fixtures"


def cli(*args, input=None):
    return CliRunner().invoke(app, [str(a) for a in args], input=input)


@pytest.fixture(scope="module")
def base_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("base") / "run"
    assert capture((FIXTURES / "candidates-landmarks.html").resolve().as_uri(), out, width=1000, height=700).status == "complete"
    assert cli("detect", out).exit_code == 0
    return out


@pytest.fixture
def run(base_run, tmp_path):
    copy = tmp_path / "run"
    shutil.copytree(base_run, copy)
    return copy


def state(run: Path) -> ReviewState:
    return ReviewState.model_validate_json((run / "review.json").read_text())


def flow_ids(run: Path) -> list[str]:
    return [r.id for r in state(run).regions if r.role == "flow"]


def digest(run: Path, names) -> dict[str, str]:
    return {n: hashlib.sha256((run / n).read_bytes()).hexdigest() for n in names}


def test_corrections_survive_restart_and_undo_restores(run):
    assert cli("review", run, "list").exit_code == 0  # starts the review
    first, second = flow_ids(run)[:2]
    assert cli("review", run, "rename", first, "Promo strip").exit_code == 0
    assert cli("review", run, "merge", first, second).exit_code == 0
    # A separate invocation (a CLI restart) sees both corrections.
    s = state(run)
    assert s.revision == 2 and s.regions[1].label == "Promo strip" and s.regions[1].id.startswith("n")
    before_undo = s.regions
    assert cli("review", run, "undo").exit_code == 0
    s = state(run)
    assert s.revision == 1 and [r.id for r in s.regions][:3] != [r.id for r in before_undo][:3]
    assert any(r.label == "Promo strip" for r in s.regions)  # the rename (revision 1) is still there


def test_review_never_changes_capture_or_proposals(run):
    protected = ["manifest.json", "elements.json", "screenshot.png", "groups.json", "candidates.json", "proposals.json"]
    before = digest(run, protected)
    cli("review", run, "list")
    ids = flow_ids(run)
    for cmd in (["rename", ids[0], "X"], ["merge", ids[0], ids[1]], ["exclude", state(run).regions[-1].id], ["undo"]):
        assert cli("review", run, *cmd).exit_code == 0
    assert cli("report", run).exit_code == 0
    assert digest(run, protected) == before


@pytest.mark.parametrize(
    "bad",
    [
        ["merge", "s0", "s9"],  # not adjacent / not flow
        ["rename", "nope", "X"],  # unknown region
        ["split", "s1", "99999"],  # outside the region
        ["adjust", "s2", "top=99999"],  # inverted edges
        ["confirm", "all"],  # no report for this revision yet
        ["teleport", "s1"],  # unknown command
    ],
)
def test_invalid_edits_leave_the_state_intact(run, bad):
    cli("review", run, "list")
    before = (run / "review.json").read_bytes()
    result = cli("review", run, *bad)
    assert "Not applied" in result.output
    assert (run / "review.json").read_bytes() == before


def test_split_preserves_membership_and_needs_explicit_allocation(run):
    cli("review", run, "list")
    elements = ElementSnapshot.model_validate_json((run / "elements.json").read_text())
    by_text = {e.text: e for e in elements.elements if e.text}
    hero = next(r for r in state(run).regions if by_text["Hero title"].id in r.element_ids)
    # Cut through the hero image: content spans the cut, so an allocation decision is required.
    image = next(e for e in elements.elements if e.tag == "img")
    y = image.bounds.y + image.bounds.height / 2
    assert "Not applied" in cli("review", run, "split", hero.id, y).output
    assert cli("review", run, "split", hero.id, y, "crossing=below").exit_code == 0
    by_id = {r.id: r for r in state(run).regions}
    upper, lower = by_id["n1"], by_id["n2"]
    assert sorted(upper.element_ids + lower.element_ids) == sorted(hero.element_ids)  # nothing lost or duplicated
    assert image.id in lower.element_ids
    assert any("manual boundary" in n for n in upper.notes)
    assert any("allocated below" in n for n in upper.notes)


def test_geometry_change_invalidates_confirmation(run):
    cli("review", run, "list")
    a, b = flow_ids(run)[:2]
    cli("review", run, "confirm", a)
    cli("review", run, "confirm", b)
    assert [r.status for r in state(run).regions if r.id in (a, b)] == ["confirmed", "confirmed"]
    cli("review", run, "merge", a, b)
    merged = state(run).regions[1]
    assert merged.status == "proposed"


def test_confirm_all_requires_the_current_report(run):
    cli("review", run, "list")
    assert cli("review", run, "report").exit_code == 0
    cli("review", run, "rename", flow_ids(run)[0], "Changed after the report")
    assert "Not applied" in cli("review", run, "confirm", "all").output  # report is one revision behind
    cli("review", run, "report")
    assert cli("review", run, "confirm", "all").exit_code == 0
    s = state(run)
    assert s.confirmation_revision == s.revision
    assert all(r.status == "confirmed" for r in s.regions)


def test_report_reflects_corrections_and_escapes_page_text(run):
    cli("review", run, "list")
    cli("review", run, "rename", flow_ids(run)[0], "<script>alert(1)</script> Pricing")
    cli("report", run)
    html = (run / "report.html").read_text()
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Pricing" in html and "<script>" not in html
    assert 'name="review-revision" content="1"' in html
    assert "data:image/png;base64," in html and "http" not in html.split("<aside>")[0]  # self-contained


def test_new_detection_requires_explicit_restart(run):
    cli("review", run, "list")
    cli("review", run, "confirm", flow_ids(run)[0])
    props = json.loads((run / "proposals.json").read_text())
    props["notes"].append("rerun")
    (run / "proposals.json").write_text(json.dumps(props))
    result = cli("review", run, "list")
    assert result.exit_code == 2 and "--restart" in result.output
    assert state(run).revision == 1  # confirmed work untouched
    assert cli("review", run, "--restart", "list").exit_code == 0
    assert state(run).revision == 0 and list(run.glob("review-*-r1.json"))  # archived, not deleted


def test_interactive_session(run):
    result = cli("review", run, input="list\nrename s1 Intro\nbogus\nundo\nfinish\n")
    assert result.exit_code == 0, result.output
    assert "Saved revision 1" in result.output and "Not applied" in result.output and "Restored revision 0" in result.output
    assert "Review saved at revision 0" in result.output

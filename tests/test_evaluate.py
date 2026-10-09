from pathlib import Path

import pytest
from typer.testing import CliRunner

from section_scan.candidates import generate
from section_scan.capture import capture
from section_scan.cli import app
from section_scan.detector import detect
from section_scan.evaluate import Annotation, evaluate
from section_scan.models import DetectorConfig, ElementSnapshot
from section_scan.normalize import normalize

FIXTURES = Path(__file__).parent.parent / "fixtures"
ANNOTATIONS = Path(__file__).parent.parent / "evaluation" / "annotations"


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    out = tmp_path_factory.mktemp("eval") / "run"
    assert capture((FIXTURES / "normalize-base.html").resolve().as_uri(), out, width=1000, height=700).status == "complete"
    elements = ElementSnapshot.model_validate_json((out / "elements.json").read_text())
    groups = normalize(elements)
    return out, elements, detect(elements, groups, generate(groups), DetectorConfig())


def ann(*sections):
    return Annotation(page="t", annotator="test", sections=[list(s) for s in sections])


def test_matching_annotation_scores_clean(base):
    _, elements, props = base
    r = evaluate(elements, props, Annotation.model_validate_json((ANNOTATIONS / "normalize-base.json").read_text()))
    assert r.missed_boundaries == [] and r.false_splits == [] and r.unresolved_anchors == []


def test_merged_expected_sections_count_as_missed(base):
    _, elements, props = base
    r = evaluate(elements, props, ann(["Hero title"], ["Hero text"]))  # really one section
    assert len(r.missed_boundaries) == 1 and r.false_splits == []


def test_one_expected_section_across_proposals_is_a_false_split(base):
    _, elements, props = base
    r = evaluate(elements, props, ann(["Home"], ["Hero title", "Footer text"]))
    assert len(r.false_splits) == 1 and r.estimated_edits >= 1


def test_unknown_anchor_is_reported_not_ignored(base):
    _, elements, props = base
    assert evaluate(elements, props, ann(["No such text"])).unresolved_anchors == ["No such text"]


def test_evaluate_compares_three_modes_without_touching_run_files(base):
    out, *_ = base
    before = sorted(p.name for p in out.iterdir())
    result = CliRunner().invoke(app, ["evaluate", str(out), str(ANNOTATIONS / "normalize-base.json")])
    assert result.exit_code == 0, result.output
    assert all(m in result.output for m in ("combined", "container", "gaps"))
    assert sorted(p.name for p in out.iterdir()) == before


def test_baseline_modes_really_differ(tmp_path):
    out = tmp_path / "run"
    capture((FIXTURES / "candidates-landmarks.html").resolve().as_uri(), out, width=1000, height=700)
    elements = ElementSnapshot.model_validate_json((out / "elements.json").read_text())
    groups = normalize(elements)
    cands = generate(groups)
    annotation = Annotation.model_validate_json((ANNOTATIONS / "candidates-landmarks.json").read_text())
    results = {
        mode: evaluate(elements, detect(elements, groups, cands, DetectorConfig(mode=mode)), annotation)
        for mode in ("combined", "container", "gaps")
    }
    # Gap evidence finds the boundary between two transparent, unboxed regions that structure misses...
    assert results["combined"].missed_boundaries == []
    assert any("'Plain text' and 'Article'" in m for m in results["container"].missed_boundaries)
    # ...and gaps alone miss a page-wide colour change that structure catches.
    assert any("'Part A' and 'Part B'" in m for m in results["gaps"].missed_boundaries)


def test_extra_sections_are_counted(base):
    _, elements, props = base
    r = evaluate(elements, props, ann(["Home"], ["Footer text"]))  # hero and features are unannotated
    assert len(r.extra_sections) == 2 and r.missed_boundaries == [] and r.false_splits == []


def test_evaluate_rejects_an_invalid_config_cleanly(base, tmp_path):
    out, *_ = base
    bad = tmp_path / "bad.toml"
    bad.write_text("full_width = 1.5\n")
    result = CliRunner().invoke(app, ["evaluate", str(out), str(ANNOTATIONS / "normalize-base.json"), "--config", str(bad)])
    assert result.exit_code == 2 and "Invalid config" in result.output

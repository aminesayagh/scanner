import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from section_scan.candidates import generate
from section_scan.capture import capture
from section_scan.cli import app
from section_scan.detector import HIDDEN_MIN_HEIGHT, detect
from section_scan.models import DetectorConfig, ElementSnapshot, ProposalSnapshot
from section_scan.normalize import normalize

FIXTURES = Path(__file__).parent.parent / "fixtures"


def capture_html(tmp_path: Path, body: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><html><head><meta charset='utf-8'></head>"
                    f"<body style='margin: 0; font: 16px/20px sans-serif; background: rgb(255, 255, 255)'>{body}</body></html>")
    out = tmp_path / "run"
    m = capture(page.as_uri(), out, width=1000, height=700)
    assert m.status == "complete", m.error
    return out


def run_detector(out: Path, config: DetectorConfig = DetectorConfig()):
    elements = ElementSnapshot.model_validate_json((out / "elements.json").read_text())
    groups = normalize(elements)
    cands = generate(groups)
    return elements, groups, cands, detect(elements, groups, cands, config)


def section_of(elements, props, text):
    el = next(e.id for e in elements.elements if e.text == text)
    return next(s for s in props.sections if el in s.element_ids)


def two_blocks(gap_px: int, inner_px: int = 60, extra_a: str = "", extra_b: str = "") -> str:
    """Two unboxed blocks of three paragraphs; internal spacing inner_px, gap_px between the blocks."""
    p = lambda t, last=False: f"<p style='margin: 0 0 {0 if last else inner_px}px'>{t}</p>"  # noqa: E731
    return (f"<div style='margin-bottom: {gap_px}px{extra_a}'>{p('A one')}{p('A two')}{p('A three', True)}</div>"
            f"<div style='{extra_b}'>{p('B one')}{p('B two')}{p('B three', True)}</div>")


def boundary_between(elements, cands, props, above_text, below_text):
    a, b = section_of(elements, props, above_text), section_of(elements, props, below_text)
    return a, b, next(
        x for x in props.boundaries if x.above_region_id in a.region_ids and x.below_region_id in b.region_ids
    )


def test_gap_is_judged_relative_to_internal_spacing(tmp_path):
    # 100px between blocks whose own spacing is 60px: ratio ~1.7, the blocks read as one section.
    elements, _, cands, props = run_detector(capture_html(tmp_path / "close", two_blocks(100)))
    assert section_of(elements, props, "A one") is section_of(elements, props, "B one")
    b = props.boundaries[0]
    assert b.decision == "keep" and b.gap == 100 and b.spacing_above.value == 60 and b.gap_ratio < 3
    # 300px with the same internal spacing: ratio 5 is a strong gap, but alone it is only ambiguous.
    elements, _, cands, props = run_detector(capture_html(tmp_path / "far", two_blocks(300)))
    b = props.boundaries[0]
    assert b.gap_ratio == pytest.approx(5) and b.decision == "ambiguous"
    assert "strong gap without independent support" in b.rules[-1]


def test_large_internal_hero_spacing_does_not_split_the_hero(tmp_path):
    # The hero's call-to-action panel is painted (narrower than the page), so the hero holds a level-1
    # candidate boundary across 240px of internal whitespace: a big gap, but no section transition.
    p = "<p style='margin: 0 0 24px'>{}</p>"
    elements, _, cands, props = run_detector(capture_html(tmp_path, f"""
        <div style="padding: 60px 40px; background: rgb(20, 20, 40); color: white">
          <div>{p.format("Headline")}{p.format("Intro one")}<p style="margin: 0">Intro two</p></div>
          <div style="width: 600px; margin: 240px auto 0; padding: 24px; background: rgb(60, 60, 110)">
            {p.format("Call to action")}<p style="margin: 0">Start now</p></div>
        </div>
        <div style="padding: 60px 40px">{p.format("Features")}<p style="margin: 0">Text</p></div>"""))
    inner = [b for b in props.boundaries if b.level == 1]
    assert len(inner) == 1 and inner[0].gap_ratio > 3  # a strong relative gap really is inside the hero...
    assert inner[0].decision != "split"  # ...but without a section transition it does not split it
    hero = section_of(elements, props, "Headline")
    assert section_of(elements, props, "Call to action") is hero
    assert section_of(elements, props, "Features") is not hero


def test_closely_adjacent_sections_split_on_a_page_wide_background_change(tmp_path):
    elements, _, cands, props = run_detector(capture_html(tmp_path, """
        <div style="background: rgb(200, 30, 30)"><h2 style="margin: 0">Red</h2><p style="margin: 0">r</p></div>
        <div style="background: rgb(30, 30, 200)"><h2 style="margin: 0">Blue</h2><p style="margin: 0">b</p></div>"""))
    red, blue, b = boundary_between(elements, cands, props, "Red", "Blue")
    assert red is not blue and b.decision == "split"
    assert b.gap == 0 and b.basis == "visual"


def test_same_background_sections_are_not_automatically_merged(tmp_path):
    body = two_blocks(160, inner_px=16, extra_a="; padding: 40px", extra_b="padding: 40px")
    elements, _, cands, props = run_detector(capture_html(tmp_path, body))
    a, b_, b = boundary_between(elements, cands, props, "A one", "B one")
    assert a is not b_ and b.decision == "split" and b.basis == "gap"
    assert any("meaningful sibling transition" in s for s in b.supporting)


def test_degenerate_measurements_stay_unknown_not_zero(tmp_path):
    elements, _, cands, props = run_detector(capture_html(tmp_path, """
        <div style="padding: 0 40px"><p style="margin: 0">Only line</p></div>
        <div style="margin-top: -10px; padding: 0 40px"><p style="margin: 0">Overlapping line</p></div>"""))
    b = props.boundaries[0]
    assert b.gap == 0  # overlapping content: the non-negative gap is zero, no division happened
    assert b.spacing_above.value is None and b.spacing_below.value is None  # unknown, not zero
    assert b.gap_ratio is None and b.spacing_scale is None and b.decision == "ambiguous"
    fields = json.dumps(b.model_dump())
    assert "probability" not in fields and "confidence" not in fields


def test_repeated_card_rows_stay_one_section(tmp_path):
    card = "<div style='padding: 20px; border: 1px solid #ccc'><h3 style='margin: 0'>{}</h3></div>"
    elements, _, cands, props = run_detector(capture_html(tmp_path, (
        "<div style='display: grid; grid-template-columns: 1fr 1fr; gap: 80px 20px; padding: 20px'>"
        + "".join(card.format(f"Card {n}") for n in range(1, 5)) + "</div>")))
    assert section_of(elements, props, "Card 1") is section_of(elements, props, "Card 4")
    assert any("repeated collection" in r for b in props.boundaries for r in b.rules)


def test_spanning_content_makes_a_split_ambiguous(tmp_path):
    elements, _, cands, props = run_detector(capture_html(tmp_path, """
        <div style="position: relative">
          <div style="height: 300px; background: rgb(200, 30, 30)"><h2 style="margin: 0">Top</h2></div>
          <div style="height: 300px; background: rgb(30, 30, 200)"><h2 style="margin: 0">Bottom</h2></div>
          <img alt="" style="position: absolute; top: 200px; left: 400px" width="200" height="200"
               src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><rect width='200' height='200' fill='gray'/></svg>">
        </div>"""))
    top, bottom, b = boundary_between(elements, cands, props, "Top", "Bottom")
    assert b.decision == "ambiguous" and "foreground content spans the boundary" in b.opposing
    assert top.ambiguous and bottom.ambiguous
    image = next(e.id for e in elements.elements if e.tag == "img")
    assert any(image in s.element_ids and any("allocated" in n for n in s.notes) for s in props.sections)


def test_landmark_page_end_to_end(tmp_path):
    out = tmp_path / "run"
    capture((FIXTURES / "candidates-landmarks.html").resolve().as_uri(), out, width=1000, height=700)
    elements, groups, cands, props = run_detector(out)
    assert props.status == "ok"
    roles = [s.role for s in props.sections]
    assert roles[0] == "header" and "footer" in roles and roles[-1] == "overlay"
    find = lambda t: section_of(elements, props, t)  # noqa: E731
    assert find("Part A") is not find("Part B")  # inner alternative decided: split
    assert find("Hero title") is not find("Features")
    assert find("Main column") is find("Side column")  # one row
    # Membership: no group in two sections; the rest is listed, never dropped.
    by_id = {g.id: g for g in groups.groups}

    def subtree(gid):
        return [gid] + [x for c in by_id[gid].child_ids for x in subtree(c)]

    claimed = [g for s in props.sections for gid in s.group_ids for g in subtree(gid)]
    assert len(claimed) == len(set(claimed))
    assert set(claimed) | set(props.unassigned_group_ids) == set(by_id)
    assert all(by_id[g].kind != "content" for g in props.unassigned_group_ids)  # only scaffolding


def test_nested_alternatives_are_promoted_level_by_level(tmp_path):
    # Divs only: the content wrapper is one level-0 region, the bundle is a level-1 region inside it,
    # and its two differently painted parts are level-2 alternatives.
    out = tmp_path / "run"
    capture((FIXTURES / "candidates-divs.html").resolve().as_uri(), out, width=1000, height=700)
    elements, groups, cands, props = run_detector(out)
    find = lambda t: section_of(elements, props, t)  # noqa: E731
    a, b = find("Part A"), find("Part B")
    assert a is not b
    inner = next(x for x in props.boundaries if x.above_region_id in a.region_ids and x.below_region_id in b.region_ids)
    assert inner.level == 2 and inner.decision == "split"
    regions = {r.id: r for r in cands.regions}
    assert all(regions[r.parent_region_id].level == r.level - 1 for r in cands.regions if r.parent_region_id)
    assert find("Three") is not a and find("Plain") is not b  # neighbours stay their own sections
    flow = [s.bounds.y for s in props.sections if s.role == "flow"]
    assert flow == sorted(flow)  # top to bottom


def test_no_main_content_is_unsupported_not_empty(tmp_path):
    _, _, _, props = run_detector(capture_html(tmp_path, (
        "<div style='position: fixed; bottom: 0; width: 300px'><p>Cookie notice</p></div>")))
    assert props.status == "unsupported"
    assert any("not a page without content" in n for n in props.notes)


def test_config_changes_outcome_and_is_recorded(tmp_path):
    out = capture_html(tmp_path, two_blocks(160, inner_px=16, extra_a="; padding: 40px", extra_b="padding: 40px"))
    cfg = tmp_path / "detector.toml"
    cfg.write_text("gap_ratio_threshold = 20.0\n")
    result = CliRunner().invoke(app, ["detect", str(out), "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    props = ProposalSnapshot.model_validate_json((out / "proposals.json").read_text())
    assert props.config.gap_ratio_threshold == 20.0 and props.config.fallback_spacing_floor == 16.0
    assert props.boundaries[0].decision == "keep"  # ratio ~8 is no longer strong at threshold 20

    cfg.write_text("gap_ratio_treshold = 2\n")  # typo: unknown keys are rejected, not ignored
    result = CliRunner().invoke(app, ["detect", str(out), "--config", str(cfg)])
    assert result.exit_code == 2 and "Invalid config" in result.output


def test_detection_is_repeatable(tmp_path):
    out = capture_html(tmp_path, two_blocks(160, inner_px=16, extra_a="; padding: 40px", extra_b="padding: 40px"))
    runs = []
    for _ in range(2):
        assert CliRunner().invoke(app, ["detect", str(out)]).exit_code == 0
        runs.append((out / "proposals.json").read_bytes())
    assert runs[0] == runs[1]
    result = CliRunner().invoke(app, ["overlay", str(out), "--sections"])
    assert result.exit_code == 0 and "Section 1" in (out / "overlay.html").read_text()


def test_narrow_painted_card_is_not_a_section_transition(tmp_path):
    elements, _, cands, props = run_detector(capture_html(tmp_path, """
        <section style="padding: 40px">
          <div><h2 style="margin: 0">Steps</h2></div>
          <div style="width: 600px; margin: 0 auto; padding: 20px; background: rgb(150, 200, 240)"><p style="margin: 0">Step card</p></div>
        </section>
        <section style="padding: 40px"><h2 style="margin: 0">Next section</h2></section>"""))
    steps = section_of(elements, props, "Steps")
    assert section_of(elements, props, "Step card") is steps  # the card stays inside its section...
    assert steps.ambiguous  # ...and the undecided inner alternative is flagged for review
    inner = next(b for b in props.boundaries if b.level == 1)
    assert inner.basis != "visual" and any("narrower-than-page" in o for o in inner.opposing)


def test_promoted_sidebar_layout_keeps_the_sidebar(tmp_path):
    elements, _, cands, props = run_detector(capture_html(tmp_path, """
        <header style="height: 60px"><p>Site</p></header>
        <div style="display: flex">
          <main style="flex: 1">
            <section style="height: 300px"><h2>Post one</h2></section>
            <section style="height: 300px"><h2>Post two</h2></section>
          </main>
          <aside style="width: 250px"><p>Sidebar</p></aside>
        </div>
        <footer style="height: 80px"><p>Foot</p></footer>"""))
    one, two = section_of(elements, props, "Post one"), section_of(elements, props, "Post two")
    assert one is not two  # main's sections were promoted
    side = section_of(elements, props, "Sidebar")
    assert side not in (one, two)
    assert any("side content" in n for n in props.notes)
    # Beside the posts, not over them: vertical ranges overlap, areas do not.
    assert side.bounds.y < two.bounds.y + two.bounds.height and one.bounds.y < side.bounds.y + side.bounds.height


@pytest.mark.parametrize(
    "body, own_section",
    [
        # A transparent header nested in the hero wrapper (common theme pattern).
        ("<div style='position: relative; background: rgb(20, 20, 40)'>"
         "<header style='position: absolute; top: 0; left: 0; right: 0; height: 60px'><p>Site header</p></header>"
         "<div style='height: 500px'><h1>Hero</h1></div></div>"
         "<div style='height: 400px'><h2>Next</h2></div>", "Site header"),
        # A fixed chat button inside a section.
        ("<section style='height: 500px'><h2>One</h2>"
         "<div style='position: fixed; right: 10px; bottom: 10px'><p>Chat</p></div></section>"
         "<section style='height: 400px'><h2>Two</h2></section>", "Chat"),
        # A footer wrapped together with a call-to-action.
        ("<section style='height: 500px'><h2>One</h2></section>"
         "<div><div style='height: 200px'><p>Final call</p></div><footer style='padding: 40px'><p>Footer text</p></footer></div>",
         "Footer text"),
    ],
)
def test_nested_header_footer_or_overlay_is_carved_out_not_a_crash(tmp_path, body, own_section):
    elements, groups, cands, props = run_detector(capture_html(tmp_path, body))
    owner = section_of(elements, props, own_section)
    assert owner.role in ("header", "footer", "overlay")
    assert any("carved out" in n for s in props.sections for n in s.notes)
    elements_seen = [e for s in props.sections for e in s.element_ids]
    assert len(elements_seen) == len(set(elements_seen))  # no element in two sections


def test_promoted_layout_orders_side_content_and_allocates_loose_badges(tmp_path):
    elements, _, cands, props = run_detector(capture_html(tmp_path, """
        <header style="height: 60px"><p>Site</p></header>
        <div style="display: flex; position: relative">
          <main style="flex: 1">
            <section style="height: 300px"><h2>Post one</h2></section>
            <section style="height: 300px"><h2>Post two</h2></section>
          </main>
          <aside style="width: 250px"><p>Sidebar</p></aside>
          <span style="position: absolute; top: 20px; left: 20px">Badge</span>
        </div>
        <footer style="height: 80px"><p>Foot</p></footer>"""))
    side, badge = section_of(elements, props, "Sidebar"), section_of(elements, props, "Badge")
    assert badge is section_of(elements, props, "Post one")  # allocated by overlap, not lumped with the sidebar
    assert any("allocated" in n for n in badge.notes)
    footer = next(s for s in props.sections if s.role == "footer")
    assert side.order < footer.order  # side content sits among the flow sections, not after the footer
    assert [s.order for s in props.sections] == list(range(len(props.sections)))


def test_detector_toml_in_working_directory_applies_by_default(tmp_path, monkeypatch):
    out = capture_html(tmp_path / "cap", two_blocks(160, inner_px=16, extra_a="; padding: 40px", extra_b="padding: 40px"))
    work = tmp_path / "work"
    work.mkdir()
    (work / "detector.toml").write_text("gap_ratio_threshold = 20.0\n")
    monkeypatch.chdir(work)
    result = CliRunner().invoke(app, ["detect", str(out)])
    assert result.exit_code == 0 and "Using config detector.toml" in result.output
    assert ProposalSnapshot.model_validate_json((out / "proposals.json").read_text()).config.gap_ratio_threshold == 20.0


def reveal_page(card_style: str) -> str:
    """A tall section whose lower half is a stack of cards in `card_style`, between two plain sections.

    With opacity 0 this is how scroll-reveal libraries leave content before it is scrolled into view.
    """
    card = ("<div style='height: 220px; margin-top: 40px; padding: 20px; background: rgb(230, 240, 255); "
            f"{card_style}'><h3 style='margin: 0'>Card {{}}</h3><p>Card text</p></div>")
    return (
        "<section style='padding: 40px; background: rgb(250, 250, 250)'><h1>Intro</h1><p>Opening text</p></section>"
        "<section style='padding: 40px; background: rgb(255, 255, 255)'>"
        "<h2 style='margin: 0'>Steps</h2><p>Visible copy above the cards</p>"
        # A hidden tooltip, a hidden layer behind visible text and screen-reader text: never limitations.
        "<span style='opacity: 0; display: inline-block; height: 100px'>Tooltip</span>"
        "<div style='position: relative'><div style='position: absolute; inset: 0; visibility: hidden'>Layer</div>"
        + "".join(f"<p style='margin: 0 0 40px'>Covered line {n}</p>" for n in range(10))
        + "</div><span style='position: absolute; width: 1px; height: 1px; overflow: hidden'>Screen reader</span>"
        + "".join(card.format(n) for n in range(1, 4))
        + "</section>"
        "<section style='padding: 40px; background: rgb(240, 250, 240)'><h2>After</h2><p>Closing text</p></section>"
    )


def test_scroll_reveal_content_is_recorded_as_hidden_content(tmp_path):
    elements, _, _, props = run_detector(capture_html(tmp_path / "hidden", reveal_page("opacity: 0")))
    steps = section_of(elements, props, "Steps")
    cards = [e.bounds for e in elements.elements if e.tag == "div" and e.visibility.opacity == 0]
    (limit,) = steps.limitations
    assert limit.kind == "hidden_content" and limit.element_count == 9  # 3 cards, each with a heading and text
    # One band from the first hidden card to the last, the gaps between cards included.
    assert (limit.top, limit.bottom) == (cards[0].y, cards[-1].y + cards[-1].height)
    assert all(not s.limitations for s in props.sections if s is not steps)


def test_page_without_hidden_content_gets_no_limitation(tmp_path):
    # The same page with the cards shown: the hidden tooltip, layer and screen-reader text alone flag nothing.
    elements, _, _, props = run_detector(capture_html(tmp_path, reveal_page("")))
    layer = next(e for e in elements.elements if e.text == "Layer")
    assert not layer.visibility.visible and layer.bounds.height >= HIDDEN_MIN_HEIGHT  # tall, but covered
    assert props.status == "ok" and all(not s.limitations for s in props.sections)


def test_hidden_content_is_shown_in_review_report_and_export(tmp_path):
    out = capture_html(tmp_path, reveal_page("opacity: 0"))
    assert CliRunner().invoke(app, ["detect", str(out)]).exit_code == 0
    props = ProposalSnapshot.model_validate_json((out / "proposals.json").read_text())
    steps = next(s for s in props.sections if s.limitations)
    listed = CliRunner().invoke(app, ["review", str(out), "list"]).output
    assert "hidden content" in listed
    inspected = CliRunner().invoke(app, ["review", str(out), "inspect", steps.id]).output
    assert "limitation: hidden content at y" in inspected and "not inspected, not empty" in inspected
    assert CliRunner().invoke(app, ["report", str(out)]).exit_code == 0
    html = (out / "report.html").read_text()
    assert "hidden content at y" in html and "· hidden content" in html
    exported = tmp_path / "draft.json"
    assert CliRunner().invoke(app, ["export", str(out), "--draft", "-o", str(exported)]).exit_code == 0
    regions = json.loads(exported.read_text())["regions"]
    assert [r["limitations"] for r in regions if r["id"] == steps.id] == [[x.model_dump() for x in steps.limitations]]

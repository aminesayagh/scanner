from pathlib import Path

import pytest
from typer.testing import CliRunner

from section_scan.candidates import generate
from section_scan.capture import capture
from section_scan.cli import app
from section_scan.models import CandidateSnapshot, ElementSnapshot, GroupSnapshot, ProposalSnapshot
from section_scan.normalize import normalize

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load(tmp_path_factory, name: str):
    out = tmp_path_factory.mktemp(name) / "run"
    m = capture((FIXTURES / f"{name}.html").resolve().as_uri(), out, width=1000, height=700)
    assert m.status == "complete", m.error
    elements = ElementSnapshot.model_validate_json((out / "elements.json").read_text())
    groups = normalize(elements)
    return out, elements, groups, generate(groups)


@pytest.fixture(scope="module")
def landmarks(tmp_path_factory):
    return load(tmp_path_factory, "candidates-landmarks")


@pytest.fixture(scope="module")
def divs(tmp_path_factory):
    return load(tmp_path_factory, "candidates-divs")


def region_of_text(elements, groups, cands, text, level=0):
    """The region at `level` whose groups contain the element with this text."""
    el = next(e.id for e in elements.elements if e.text == text)
    by_id = {g.id: g for g in groups.groups}
    owner = next(g for g in groups.groups if el in g.element_ids)
    chain = {owner.id}
    while owner.parent_id:
        owner = by_id[owner.parent_id]
        chain.add(owner.id)
    return next(r for r in cands.regions if r.level == level and chain & set(r.group_ids))


def boundary(cands, above, below):
    return next(b for b in cands.boundaries if (b.above_region_id, b.below_region_id) == (above.id, below.id))


def spans(regions):
    return sorted((round(r.bounds.y), round(r.bounds.height)) for r in regions)


def test_structure_from_landmarks(landmarks):
    _, _, _, cands = landmarks
    roles = [r.role for r in cands.regions if r.level == 0]
    assert roles.count("header") == 1 and roles.count("footer") == 1
    assert cands.structure.main_group_id is not None
    # Inside the #page wrapper: the promo bar, then <main> opened up into its 5 regions.
    flow = [r for r in cands.regions if r.level == 0 and r.role == "flow"]
    assert len(flow) == 6
    assert any("holds the main landmark" in n for n in cands.structure.notes)


def test_candidates_found_without_landmark_tags(landmarks, divs):
    _, _, _, with_tags = landmarks
    _, d_el, d_gr, without = divs
    assert any("fallback" in n for n in without.structure.notes)
    assert any("main: no landmark" in n for n in without.structure.notes)
    assert any("looked inside" in n for n in without.structure.notes)  # the #page wrapper
    assert [r.role for r in without.regions if r.level == 0][0] == "header"  # found by the fallback
    # Without <main>/<section>, the content wrapper is one candidate; its regions are kept as alternatives.
    wrapper = region_of_text(d_el, d_gr, without, "Hero title")
    nested = [r for r in without.regions if r.parent_region_id == wrapper.id]
    promo = region_of_text(d_el, d_gr, without, "Promo bar")
    tagged_flow = [r for r in with_tags.regions if r.level == 0 and r.role == "flow" and r.bounds.y > promo.bounds.y]
    assert spans(nested) == spans(tagged_flow)


def test_multi_column_hero_is_one_region(landmarks):
    _, elements, groups, cands = landmarks
    hero = region_of_text(elements, groups, cands, "Hero title")
    image = next(e for e in elements.elements if e.tag == "img")
    b = hero.bounds
    assert b.y <= image.bounds.y and image.bounds.y + image.bounds.height <= b.y + b.height
    assert not any(b.y < x.y < b.y + b.height for x in cands.boundaries if x.level == 0)


def test_side_by_side_siblings_form_one_band(landmarks):
    _, elements, groups, cands = landmarks
    article = region_of_text(elements, groups, cands, "Main column")
    assert article is region_of_text(elements, groups, cands, "Side column")
    assert len(article.group_ids) == 2 and "row of 2 side-by-side groups" in article.notes


def test_header_overlap_is_explicit(landmarks):
    _, elements, groups, cands = landmarks
    header = next(r for r in cands.regions if r.role == "header")
    hero = region_of_text(elements, groups, cands, "Hero title")
    assert any(set(o.region_ids) == {header.id, hero.id} and o.kind == "header_over_content" for o in cands.overlaps)


def test_fixed_banner_is_an_overlay(landmarks):
    _, elements, groups, cands = landmarks
    cookie = region_of_text(elements, groups, cands, "We use cookies")
    assert cookie.role == "overlay"
    assert any(cookie.id in o.region_ids and o.kind == "overlay" for o in cands.overlaps)


def test_boundary_evidence(landmarks):
    _, elements, groups, cands = landmarks
    hero, features, plain, cols = (
        region_of_text(elements, groups, cands, t) for t in ("Hero title", "Features", "Plain", "Main column")
    )
    hf = boundary(cands, hero, features)
    assert hf.evidence.background_change is True
    assert hf.evidence.foreground_spans  # the badge pokes up into the hero
    assert hf.evidence.landmarks == ("section", "section")
    pc = boundary(cands, plain, cols)
    assert pc.evidence.background_change is False and pc.evidence.shared_painted_ancestor is not None
    assert pc.content_gap > pc.box_gap  # padding: content sits further apart than the boxes


def test_ambiguous_alternatives_are_retained(landmarks):
    _, elements, groups, cands = landmarks
    bundle = region_of_text(elements, groups, cands, "Part A")
    assert bundle is region_of_text(elements, groups, cands, "Part B")  # one level-0 candidate...
    parts = [r for r in cands.regions if r.parent_region_id == bundle.id]
    assert len(parts) == 2  # ...with the split kept as a level-1 alternative
    inner = boundary(cands, parts[0], parts[1])
    assert inner.level == 1 and inner.evidence.background_change is True


def test_detect_writes_candidates_and_overlay_draws_regions(landmarks):
    out, *_ = landmarks
    result = CliRunner().invoke(app, ["detect", str(out)])
    assert result.exit_code == 0, result.output
    cands = CandidateSnapshot.model_validate_json((out / "candidates.json").read_text())
    result = CliRunner().invoke(app, ["overlay", str(out), "--regions"])
    assert result.exit_code == 0, result.output
    html = (out / "overlay.html").read_text()
    assert all(f">{r.id} {r.role}<" in html for r in cands.regions if r.level == 0)


def test_tall_header_landmark_is_treated_as_content(tmp_path):
    page = tmp_path / "hero-header.html"
    page.write_text(
        "<!doctype html><body style='margin:0'>"
        "<nav style='position:sticky;top:0;height:50px;background:#eee'>Site nav</nav>"
        "<header style='height:500px;background:#123;color:#fff'><h1>Big hero</h1></header>"
        "<main><section style='height:400px'><h2>Body</h2></section></main></body>"
    )
    out = tmp_path / "r"
    assert capture(page.as_uri(), out, width=1000, height=700).status == "complete"
    elements = ElementSnapshot.model_validate_json((out / "elements.json").read_text())
    groups = normalize(elements)
    cands = generate(groups)
    assert any("rejected" in n for n in cands.structure.notes)
    header = next(r for r in cands.regions if r.role == "header")
    assert region_of_text(elements, groups, cands, "Site nav") is header
    assert region_of_text(elements, groups, cands, "Big hero").role == "flow"


def test_config_header_max_height_changes_header_choice(tmp_path):
    # The 500px <header> is a hero by default; allowing taller headers in detector.toml makes it the header.
    page = tmp_path / "hero-header.html"
    page.write_text(
        "<!doctype html><body style='margin:0'>"
        "<header style='height:500px;background:#123;color:#fff'><h1>Big hero</h1></header>"
        "<main><section style='height:400px'><h2>Body</h2></section></main></body>"
    )
    out = tmp_path / "r"
    assert capture(page.as_uri(), out, width=1000, height=700).status == "complete"
    elements = ElementSnapshot.model_validate_json((out / "elements.json").read_text())
    groups = normalize(elements)
    assert region_of_text(elements, groups, generate(groups), "Big hero").role == "flow"

    cfg = tmp_path / "detector.toml"
    cfg.write_text("header_max_height = 600.0\n")
    result = CliRunner().invoke(app, ["detect", str(out), "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    groups = GroupSnapshot.model_validate_json((out / "groups.json").read_text())
    cands = CandidateSnapshot.model_validate_json((out / "candidates.json").read_text())
    props = ProposalSnapshot.model_validate_json((out / "proposals.json").read_text())
    assert region_of_text(elements, groups, cands, "Big hero").role == "header"
    # Every stage records the configuration it ran with.
    assert groups.config.header_max_height == cands.config.header_max_height == props.config.header_max_height == 600


def detect_html(tmp_path, html: str):
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><html><head><meta charset='utf-8'><style>* { margin: 0 }</style></head>"
                    f"<body style='font: 16px sans-serif; background: rgb(255, 255, 255)'>{html}</body></html>")
    out = tmp_path / "r"
    m = capture(page.as_uri(), out, width=1000, height=700)
    assert m.status == "complete", m.error
    elements = ElementSnapshot.model_validate_json((out / "elements.json").read_text())
    groups = normalize(elements)
    return elements, groups, generate(groups)


def test_blog_sidebar_keeps_main_sections_as_alternatives(tmp_path):
    elements, groups, cands = detect_html(tmp_path, """
        <header style="height: 60px"><p>Site</p></header>
        <div style="display: flex">
          <main style="flex: 1">
            <section style="height: 300px"><h2>Post one</h2></section>
            <section style="height: 300px"><h2>Post two</h2></section>
            <section style="height: 300px"><h2>Post three</h2></section>
          </main>
          <aside style="width: 250px"><p>Sidebar</p></aside>
        </div>
        <footer style="height: 80px"><p>Foot</p></footer>""")
    row = region_of_text(elements, groups, cands, "Sidebar")
    assert row is region_of_text(elements, groups, cands, "Post one")  # main column + sidebar: one row
    assert any("kept" in n and "side content" in n for n in cands.structure.notes)
    posts = [r for r in cands.regions if r.parent_region_id == row.id]
    assert [region_of_text(elements, groups, cands, f"Post {n}", level=1).id for n in ("one", "two", "three")] == [
        r.id for r in posts
    ]
    footer = next(r for r in cands.regions if r.role == "footer")
    assert any(b.below_region_id == footer.id for b in cands.boundaries)  # footer transition is recorded


def test_negative_margin_card_is_a_stacked_overlap_not_a_row(tmp_path):
    elements, groups, cands = detect_html(tmp_path, """
        <section style="height: 400px; background: rgb(20, 20, 40)"><h1 style="color: white">Hero</h1></section>
        <section style="margin-top: -80px; padding: 0 100px">
          <div style="height: 200px; background: rgb(240, 240, 240)"><p>Stats card</p></div>
        </section>
        <section style="height: 300px"><h2>After</h2></section>""")
    hero = region_of_text(elements, groups, cands, "Hero")
    card = region_of_text(elements, groups, cands, "Stats card")
    assert hero is not card
    assert any(set(o.region_ids) == {hero.id, card.id} and o.kind == "content" for o in cands.overlaps)


def test_nested_footers_are_not_the_page_footer(tmp_path):
    elements, groups, cands = detect_html(tmp_path, """
        <section style="height: 400px"><blockquote><p>Great product</p><footer>Jane, CEO</footer></blockquote></section>
        <section style="height: 400px"><h2>More</h2></section>
        <footer style="padding: 40px"><p>Real footer</p></footer>
        <div role="dialog" style="position: fixed; bottom: 0; right: 0; width: 300px">
          <p>Cookies</p><footer><button>OK</button></footer>
        </div>""")
    footer = next(r for r in cands.regions if r.role == "footer")
    assert region_of_text(elements, groups, cands, "Real footer") is footer
    assert region_of_text(elements, groups, cands, "Jane, CEO").role == "flow"


def test_backdrop_layer_is_not_a_fallback_header(tmp_path):
    elements, groups, cands = detect_html(tmp_path, """
        <div style="position: absolute; top: 0; left: 0; right: 0; height: 700px; background: rgb(20, 20, 60)"></div>
        <div style="position: sticky; top: 0; height: 50px"><p>Nav bar</p></div>
        <div style="height: 600px"><h1>Hero</h1></div>
        <div style="height: 400px"><h2>Next</h2></div>""")
    header = next(r for r in cands.regions if r.role == "header")
    assert region_of_text(elements, groups, cands, "Nav bar") is header


def test_out_of_flow_content_straddling_a_boundary_is_recorded(tmp_path):
    elements, groups, cands = detect_html(tmp_path, """
        <main style="position: relative">
          <section style="height: 400px"><h2>Top</h2></section>
          <section style="height: 400px"><h2>Bottom</h2></section>
          <img alt="Phone mockup" style="position: absolute; top: 300px; right: 40px" width="200" height="200"
               src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><rect width='200' height='200' fill='gray'/></svg>">
        </main>""")
    top, bottom = (region_of_text(elements, groups, cands, t) for t in ("Top", "Bottom"))
    assert boundary(cands, top, bottom).evidence.foreground_spans


def test_full_bleed_painted_child_colours_its_region(tmp_path):
    elements, groups, cands = detect_html(tmp_path, """
        <section style="padding: 20px 0"><div style="height: 400px; background: rgb(200, 30, 30)"><h2>Red band</h2></div></section>
        <section style="padding: 20px 0; height: 300px"><h2>White band</h2></section>""")
    red, white = (region_of_text(elements, groups, cands, t) for t in ("Red band", "White band"))
    b = boundary(cands, red, white)
    assert b.evidence.backgrounds[0] == "rgb(200, 30, 30)"
    assert b.evidence.background_change is True and b.evidence.shared_painted_ancestor is None

from pathlib import Path

import pytest
from typer.testing import CliRunner

from section_scan.capture import capture
from section_scan.cli import app
from section_scan.models import ElementSnapshot, GroupSnapshot
from section_scan.normalize import normalize

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load(tmp_path_factory, name: str) -> tuple[Path, ElementSnapshot, GroupSnapshot]:
    out = tmp_path_factory.mktemp(name) / "run"
    m = capture((FIXTURES / f"{name}.html").resolve().as_uri(), out, width=1000, height=700)
    assert m.status == "complete", m.error
    elements = ElementSnapshot.model_validate_json((out / "elements.json").read_text())
    return out, elements, normalize(elements)


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return load(tmp_path_factory, "normalize-base")


@pytest.fixture(scope="module")
def wrapped(tmp_path_factory):
    return load(tmp_path_factory, "normalize-wrapped")


def shape(elements: ElementSnapshot, groups: GroupSnapshot) -> list:
    """Group tree as (kind, tag, rounded bounds, children): what later stages will see."""
    tags = {e.id: e.tag for e in elements.elements}
    by_id = {g.id: g for g in groups.groups}

    def node(gid):
        g = by_id[gid]
        b = g.bounds
        bounds = tuple(round(v) for v in (b.x, b.y, b.width, b.height))
        return (g.kind, tags[g.primary_element_id], bounds, [node(c) for c in g.child_ids])

    return [node(g.id) for g in groups.groups if g.parent_id is None]


def group_with_text(elements, groups, text):
    owner = next(e.id for e in elements.elements if e.text == text)
    return next(g for g in groups.groups if owner in g.element_ids)


def test_redundant_wrappers_do_not_change_groups(base, wrapped):
    _, base_el, base_gr = base
    _, wrapped_el, wrapped_gr = wrapped
    assert len(wrapped_el.elements) > len(base_el.elements) + 10  # the wrappers really are there
    assert shape(wrapped_el, wrapped_gr) == shape(base_el, base_gr)


def test_every_element_is_accounted_for_once(wrapped):
    _, elements, groups = wrapped
    members = [i for g in groups.groups for i in g.element_ids] + [o.element_id for o in groups.omitted]
    assert sorted(members) == sorted(e.id for e in elements.elements)
    assert any(o.reason == "pass_through" for o in groups.omitted)  # the display: contents wrapper


def test_padded_background_wrapper_is_kept_as_evidence(base):
    _, elements, groups = base
    by_id = {g.id: g for g in groups.groups}
    inner = by_id[group_with_text(elements, groups, "Hero title").parent_id]
    panel = by_id[inner.parent_id]
    assert any(r.startswith("paints background") for r in panel.reasons)
    assert "has padding" in panel.reasons
    assert panel.evidence.background.own == "rgb(240, 240, 250)"
    assert inner.evidence.background.context == "rgb(240, 240, 250)"  # transparent: inherits the panel's paint


def test_complex_background_is_unknown_not_a_colour(base):
    _, elements, groups = base
    by_id = {g.id: g for g in groups.groups}
    features = by_id[group_with_text(elements, groups, "Features").parent_id]
    assert features.evidence.background.own == "complex"
    assert group_with_text(elements, groups, "Features").evidence.background.context == "complex"


def test_repeated_cards_stay_children_of_their_group(base):
    _, elements, groups = base
    by_id = {g.id: g for g in groups.groups}
    cards = [by_id[group_with_text(elements, groups, t).parent_id] for t in ("One", "Two", "Three")]
    grid = by_id[cards[0].parent_id]
    assert grid.child_ids == [c.id for c in cards]
    assert grid.evidence.repeated_children and grid.evidence.display == "grid"
    assert all(c.kind == "container" and "has a border" in c.reasons for c in cards)


def test_inline_text_run_is_one_content_group(base):
    _, elements, groups = base
    read_more = group_with_text(elements, groups, "Read")
    assert read_more.kind == "content"
    assert group_with_text(elements, groups, "more").id == read_more.id


def test_detect_writes_groups_and_overlay_draws_them(wrapped):
    out, _, _ = wrapped
    result = CliRunner().invoke(app, ["detect", str(out)])
    assert result.exit_code == 0, result.output
    groups = GroupSnapshot.model_validate_json((out / "groups.json").read_text())
    result = CliRunner().invoke(app, ["overlay", str(out), "--group", groups.groups[1].id])
    assert result.exit_code == 0, result.output
    assert f"top:{groups.groups[1].bounds.y}px" in (out / "overlay.html").read_text()


@pytest.fixture(scope="module")
def edge(tmp_path_factory):
    return load(tmp_path_factory, "normalize-edge")


def test_inline_block_cards_stay_separate_cards(edge):
    _, elements, groups = edge
    by_id = {g.id: g for g in groups.groups}
    cards = [by_id[group_with_text(elements, groups, f"Inline {n}").parent_id] for n in ("one", "two", "three")]
    assert all(c.kind == "container" for c in cards)
    row = by_id[cards[0].parent_id]
    assert row.child_ids == [c.id for c in cards] and row.evidence.repeated_children


def test_link_wrapped_cards_are_not_one_text_run(edge):
    _, elements, groups = edge
    one, two = (group_with_text(elements, groups, f"Linked {n}") for n in ("one", "two"))
    assert one.id != two.id and one.kind == two.kind == "content"


def test_text_beside_an_omitted_child_is_kept(edge):
    _, elements, groups = edge
    price = group_with_text(elements, groups, "Price $49")
    assert price.kind == "content"
    assert not any(o.element_id == price.primary_element_id for o in groups.omitted)


def test_absolute_wrapper_keeps_out_of_flow_positioning(edge):
    _, elements, groups = edge
    by_id = {g.id: g for g in groups.groups}
    bar = by_id[group_with_text(elements, groups, "Floating bar").parent_id]
    wrapper = by_id[bar.parent_id]
    assert wrapper.positioning == "absolute" and "positioned absolute" in wrapper.reasons


def test_semi_transparent_background_is_complex(edge):
    _, elements, groups = edge
    assert group_with_text(elements, groups, "Tinted text").evidence.background.context == "complex"


def test_stacked_full_width_sections_are_not_repeated_cards(edge):
    _, elements, groups = edge
    by_id = {g.id: g for g in groups.groups}
    stack = by_id[by_id[group_with_text(elements, groups, "Stacked one").parent_id].parent_id]
    assert len(stack.child_ids) == 3 and not stack.evidence.repeated_children


def test_meaningful_page_wrapper_is_not_replaced_by_body(tmp_path_factory):
    _, elements, groups = load(tmp_path_factory, "normalize-root")
    root = groups.groups[0]
    tags = {e.id: e.tag for e in elements.elements}
    assert tags[root.primary_element_id] == "div"
    assert root.evidence.padding and root.evidence.background.own == "rgb(250, 240, 230)"

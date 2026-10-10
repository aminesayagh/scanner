import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from section_scan.capture import capture
from section_scan.cli import app
from section_scan.models import ElementSnapshot, ExportDocument, ReviewState

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
    assert 'name="review-fingerprint"' in html
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


def test_confirm_all_rejects_a_report_of_a_different_state_with_the_same_revision(run):
    cli("review", run, "list")
    a, b = flow_ids(run)[:2]
    cli("review", run, "rename", a, "Hero")  # r1
    cli("review", run, "report")  # report shows r1: "Hero" over two separate regions
    cli("review", run, "undo")  # r0
    cli("review", run, "merge", a, b)  # r1 again, different content
    assert "Not applied" in cli("review", run, "confirm", "all").output
    assert all(r.status != "confirmed" for r in state(run).regions)


def test_merge_cannot_resurrect_excluded_content(run):
    cli("review", run, "list")
    a, b = flow_ids(run)[:2]
    cli("review", run, "exclude", b)
    assert "Not applied" in cli("review", run, "merge", a, b).output
    assert next(r for r in state(run).regions if r.id == b).status == "excluded"


@pytest.mark.parametrize("edit", ["split", "adjust"])
def test_split_and_adjust_invalidate_confirmation(run, edit):
    cli("review", run, "list")
    elements = ElementSnapshot.model_validate_json((run / "elements.json").read_text())
    plain = next(e for e in elements.elements if e.text == "Plain")
    target = next(r for r in state(run).regions if plain.id in r.element_ids)
    cli("review", run, "confirm", target.id)
    if edit == "split":
        y = plain.bounds.y + plain.bounds.height  # exactly where the heading ends and its paragraph starts
        assert cli("review", run, "split", target.id, y).exit_code == 0
        changed = [r for r in state(run).regions if r.id.startswith("n")]
    else:
        assert cli("review", run, "adjust", target.id, f"top={target.bounds.y + 1}").exit_code == 0
        changed = [r for r in state(run).regions if r.id == target.id]
    assert changed and all(r.status == "proposed" for r in changed)


def test_split_bounds_contain_allocated_content_and_stay_adjustable(run):
    cli("review", run, "list")
    elements = ElementSnapshot.model_validate_json((run / "elements.json").read_text())
    image = next(e for e in elements.elements if e.tag == "img")
    hero = next(r for r in state(run).regions if image.id in r.element_ids)
    y = image.bounds.y + image.bounds.height / 2
    assert cli("review", run, "split", hero.id, y, "crossing=below").exit_code == 0
    lower = next(r for r in state(run).regions if r.id == "n2")
    assert lower.bounds.y <= image.bounds.y  # the receiving part holds the image it was given
    bottom = lower.bounds.y + lower.bounds.height
    assert cli("review", run, "adjust", "n2", f"bottom={bottom - 1}").exit_code == 0  # moving one edge only


def test_markup_in_labels_is_shown_literally(run):
    cli("review", run, "list")
    cli("review", run, "rename", flow_ids(run)[0], "Deals [/b] [bold]x")
    result = cli("review", run, "list")
    assert result.exit_code == 0 and "[/b]" in result.output and "[bold]x" in result.output


def test_stale_review_is_not_drawn_over_new_proposals(run):
    cli("review", run, "list")
    cli("review", run, "rename", flow_ids(run)[0], "Old label")
    props = json.loads((run / "proposals.json").read_text())
    props["notes"].append("rerun")
    (run / "proposals.json").write_text(json.dumps(props))
    result = cli("report", run)
    assert "predates the current proposals" in result.output
    assert "Old label" not in (run / "report.html").read_text()


def test_interactive_unmatched_quote_does_not_end_the_session(run):
    result = cli("review", run, input="rename s1 Bob's\nrename s1 Intro\nfinish\n")
    assert result.exit_code == 0 and "Not applied" in result.output and "Saved revision 1" in result.output


def test_normal_export_requires_every_retained_region_confirmed(run, tmp_path):
    out = tmp_path / "sections.json"
    cli("review", run, "list")
    result = cli("export", run, "-o", out)
    assert result.exit_code == 1 and "still need review" in result.output and not out.exists()

    overlay = next(r for r in state(run).regions if r.role == "overlay")
    cli("review", run, "exclude", overlay.id)
    cli("review", run, "report")
    cli("review", run, "confirm", "all")
    assert cli("export", run, "-o", out).exit_code == 0
    doc = ExportDocument.model_validate_json(out.read_text())  # validates against the model
    assert doc.kind == "sections" and all(r.status == "confirmed" for r in doc.regions)
    assert overlay.id not in [r.id for r in doc.regions]  # excluded regions are not exported
    assert doc.review_revision == state(run).revision and doc.page_url.startswith("file://")
    assert any(k == "header_over_content" for r in doc.regions for _, k in r.overlaps)
    assert [r.order for r in doc.regions] == list(range(len(doc.regions)))


def test_draft_export_keeps_statuses_distinct(run, tmp_path):
    out = tmp_path / "draft.json"
    assert cli("export", run, "-o", out).exit_code == 1  # nothing reviewed: no normal export
    assert cli("export", run, "-o", out, "--draft").exit_code == 0
    doc = ExportDocument.model_validate_json(out.read_text())
    assert doc.kind == "draft-sections" and doc.review_revision is None
    assert {r.status for r in doc.regions} == {"proposed"}
    cli("review", run, "list")
    cli("review", run, "confirm", flow_ids(run)[0])
    assert cli("export", run, "-o", out, "--draft").exit_code == 0
    doc = ExportDocument.model_validate_json(out.read_text())
    assert {r.status for r in doc.regions} == {"proposed", "confirmed"}
    with pytest.raises(ValueError):  # a document claiming to be confirmed cannot carry proposed regions
        ExportDocument.model_validate({**doc.model_dump(), "kind": "sections"})


def test_export_refuses_a_stale_review(run, tmp_path):
    cli("review", run, "list")
    props = json.loads((run / "proposals.json").read_text())
    props["notes"].append("rerun")
    (run / "proposals.json").write_text(json.dumps(props))
    result = cli("export", run, "-o", tmp_path / "x.json", "--draft")
    assert result.exit_code == 1 and "predates" in result.output


def test_export_order_is_page_order_and_needs_something_to_export(run, tmp_path):
    cli("review", run, "list")
    cli("review", run, "report")
    cli("review", run, "confirm", "all")
    out = tmp_path / "new" / "dir" / "sections.json"  # missing folders are created
    assert cli("export", run, "-o", out).exit_code == 0
    doc = ExportDocument.model_validate_json(out.read_text())
    ys = [r.bounds.y for r in doc.regions]
    assert ys == sorted(ys) and any(r.role == "overlay" for r in doc.regions)
    for r in state(run).regions:
        cli("review", run, "exclude", r.id)
    result = cli("export", run, "-o", tmp_path / "empty.json")
    assert result.exit_code == 1 and "nothing to export" in result.output


def test_report_json_lists_sections_with_position_flags_role_and_message_only(run):
    cli("review", run, "list")
    overlay = next(r for r in state(run).regions if r.role == "overlay")
    cli("review", run, "exclude", overlay.id)
    assert cli("report", run).exit_code == 0
    doc = json.loads((run / "report.json").read_text())
    assert set(doc) == {"link", "title", "description", "sections"}
    assert doc["link"].startswith("file://") and doc["link"].endswith("candidates-landmarks.html")
    assert doc["title"] == "Candidates (landmarks)" and doc["description"] == "A page with every landmark case."
    assert all(set(s) == {"id", "position", "flags", "role", "message"} for s in doc["sections"])
    assert all(isinstance(s["message"], list) for s in doc["sections"])
    assert all(set(s["position"]) == {"x", "y", "width", "height"} for s in doc["sections"])
    ys = [s["position"]["y"] for s in doc["sections"]]
    assert ys == sorted(ys)  # top to bottom
    assert "overlay" not in [s["role"] for s in doc["sections"]]  # excluded in review
    assert "overlaps:header_over_content" in doc["sections"][0]["flags"]  # header drawn over the content


def test_report_json_message_lists_section_content_with_tags(run):
    cli("report", run)
    doc = json.loads((run / "report.json").read_text())
    contents = [s["message"] for s in doc["sections"]]
    # The hero's copy block and its image are sibling branches; the image counts by its alt text.
    hero = next(c for c in contents if "Hero title" in json.dumps(c))
    assert hero == [
        {"tag": "group", "children": [{"tag": "h1", "text": "Hero title"}, {"tag": "p", "text": "Hero copy"}]},
        {"tag": "img", "text": "Hero image", "src": "data:image/svg+xml"},
    ]
    # A paragraph is one entry with its inline link; the link's text is not listed again.
    # A group with no siblings is unwrapped: its items are the message.
    plain = next(c for c in contents if {"tag": "h2", "text": "Plain"} in c)
    assert plain == [{"tag": "h2", "text": "Plain"}, {"tag": "p", "text": "Plain text with a link."}]
    features = next(c for c in contents if {"tag": "h2", "text": "Features"} in c)
    assert {"tag": "p", "text": "New"} in features  # loose text on its own branch
    assert {"tag": "group", "children": [{"tag": "h3", "text": t} for t in ("One", "Two", "Three")]} in features
    every = json.dumps(contents)
    assert "Mobile menu" not in every  # hidden content is not part of the message


def test_report_json_groups_list_items_under_their_list_without_double_nesting(tmp_path):
    page = tmp_path / "lists.html"
    page.write_text("""<!doctype html><html><head><meta charset="utf-8"><title>Lists</title></head>
      <body style="margin: 0"><section style="padding: 40px">
        <h1>Plans</h1><p>Pick one of <a href="#">these</a>.</p>
        <ul><li>Starter</li><li>Pro <strong>plan</strong><ol><li>Nested one</li><li>Nested two</li></ol></li><li>Agency</li></ul>
        <p>After the list</p>
      </section>
      <section style="padding: 40px; background: rgb(30, 30, 60); color: white"><h2>Next section</h2></section>
      </body></html>""")
    out = tmp_path / "run"
    assert capture(page.as_uri(), out, width=1000, height=700).status == "complete"
    assert cli("detect", out).exit_code == 0 and cli("report", out).exit_code == 0
    content = next(s for s in json.loads((out / "report.json").read_text())["sections"] if s["role"] == "flow")["message"]
    assert content == [
        {"tag": "h1", "text": "Plans"},
        {"tag": "p", "text": "Pick one of these."},
        {"tag": "ul", "children": [
            {"tag": "li", "text": "Starter"},
            {"tag": "li", "text": "Pro plan"},
            {"tag": "li", "text": "Nested one"},
            {"tag": "li", "text": "Nested two"},
            {"tag": "li", "text": "Agency"},
        ]},
        {"tag": "p", "text": "After the list"},
    ]



def test_report_json_section_id_is_an_anchor_wrapping_all_its_content(tmp_path):
    page = tmp_path / "anchors.html"
    page.write_text("""<!doctype html><html><head><meta charset="utf-8"><title>Anchors</title></head>
      <body style="margin: 0"><div id="page">
        <section id="intro" style="padding: 40px; background: rgb(250, 230, 230)"><h1>Intro</h1><p>Hello</p></section>
        <section style="padding: 40px; background: rgb(230, 250, 230)">
          <div id="pricing"><div id="pricing-title"><h2>Pricing</h2></div><p>Plans</p>
            <img alt="" width="100" height="50" src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='100' height='50'/>"></div>
        </section>
        <section style="padding: 40px; background: rgb(230, 230, 250)"><h2 id="faq-title">FAQ</h2><p>Answers</p></section>
      </div></body></html>""")
    out = tmp_path / "run"
    assert capture(page.as_uri(), out, width=1000, height=700).status == "complete"
    assert cli("detect", out).exit_code == 0 and cli("report", out).exit_code == 0
    def first_text(content):
        entry = content[0]
        return entry["children"][0]["text"] if "children" in entry else entry["text"]

    ids = {first_text(s["message"]): s["id"] for s in json.loads((out / "report.json").read_text())["sections"]}
    assert ids["Intro"] == "intro"  # the section element itself
    assert ids["Pricing"] == "pricing"  # an inner wrapper around all the content, not the heading-only one
    assert ids["FAQ"] is None  # only the heading has an id; "page" also wraps other sections


def test_report_json_content_follows_dom_branches_one_level_deep(tmp_path):
    # The shape of masayagh.com's header: wrappers; logo + time; one wrapper holding two navs.
    page = tmp_path / "branches.html"
    page.write_text("""<!doctype html><html><head><meta charset="utf-8"><title>Branches</title></head>
      <body style="margin: 0">
        <header style="height: 60px"><div><div style="display: flex; gap: 40px">
          <div><img alt="" width="20" height="20" src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='20' height='20'/>"><time>11:26 PM, Morocco</time></div>
          <div style="display: flex; gap: 40px">
            <nav><a href="#">Home</a> <span>/</span> <a href="#">Projects</a></nav>
            <nav><a href="#">About us</a> <a href="#">Get in touch</a></nav>
          </div>
        </div></div></header>
        <section style="padding: 40px; background: rgb(240, 240, 250)">
          <div><div><h2>Plans</h2><p>Pick one</p></div>
            <ul><li>Starter</li><li>Pro<ol><li>Nested</li></ol></li></ul>
            <div><a href="#">Start free</a></div></div>
        </section>
        <section style="padding: 40px; background: rgb(30, 30, 60); color: white"><h2>Next</h2></section>
      </body></html>""")
    out = tmp_path / "run"
    assert capture(page.as_uri(), out, width=1000, height=700).status == "complete"
    assert cli("detect", out).exit_code == 0 and cli("report", out).exit_code == 0
    sections = json.loads((out / "report.json").read_text())["sections"]
    header = next(s for s in sections if s["role"] == "header")["message"]
    assert header == [
        # The logo has no alt text: it is listed by its source, beside the time in the same branch.
        {"tag": "group", "children": [{"tag": "img", "src": "data:image/svg+xml"}, {"tag": "text", "text": "11:26 PM, Morocco"}]},
        {"tag": "group", "children": [{"tag": "a", "text": "Home"}, {"tag": "text", "text": "/"},
                                      {"tag": "a", "text": "Projects"}]},
        {"tag": "group", "children": [{"tag": "a", "text": "About us"}, {"tag": "a", "text": "Get in touch"}]},
    ]
    plans = next(s for s in sections if "Plans" in json.dumps(s["message"]))
    assert plans["message"] == [
        {"tag": "group", "children": [{"tag": "h2", "text": "Plans"}, {"tag": "p", "text": "Pick one"}]},
        {"tag": "ul", "children": [{"tag": "li", "text": "Starter"}, {"tag": "li", "text": "Pro"},
                                   {"tag": "li", "text": "Nested"}]},
        {"tag": "a", "text": "Start free"},
    ]
    # One level only: no group ever holds another group.
    for s in sections:
        for entry in s["message"]:
            assert all("children" not in child for child in entry.get("children", []))


def test_report_json_list_items_keep_their_parts_including_collapsed_text(tmp_path):
    # The shape of masayagh.com's work list: each item is a closed <details> whose description shows on click.
    work = """<li><details><summary><div><p>Mar 2025 – May 2025</p><h3>SmiAfrica</h3></div></summary>
                <div><p>Contributed as a software consultant.</p></div></details></li>"""
    page = tmp_path / "work.html"
    page.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>Work</title>
      <style>.sr-only {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }}</style></head>
      <body style="margin: 0"><section style="padding: 40px">
        <h2>Selected work.</h2>
        <ul>{work}<li>Plain item</li></ul>
        <p style="visibility: hidden">Revealed by animation</p>
        <span class="sr-only">Screen reader only</span>
      </section>
      <section style="padding: 40px; background: rgb(30, 30, 60); color: white"><h2>Next</h2></section>
      </body></html>""")
    out = tmp_path / "run"
    assert capture(page.as_uri(), out, width=1000, height=700).status == "complete"
    assert cli("detect", out).exit_code == 0 and cli("report", out).exit_code == 0
    message = json.loads((out / "report.json").read_text())["sections"][0]["message"]
    assert message[0] == {"tag": "h2", "text": "Selected work."}
    assert message[1] == {"tag": "ul", "children": [
        {"tag": "li", "children": [
            {"tag": "p", "text": "Mar 2025 – May 2025"},
            {"tag": "h3", "text": "SmiAfrica"},
            {"tag": "p", "text": "Contributed as a software consultant."},  # inside the closed <details>
        ]},
        {"tag": "li", "text": "Plain item"},
    ]}
    assert {"tag": "p", "text": "Revealed by animation"} in message  # in the DOM, painted later
    assert "Screen reader only" not in json.dumps(message)


def test_report_json_lists_every_image_with_its_alt_text_and_source(tmp_path):
    svg = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='40' height='40'/>"
    page = tmp_path / "images.html"
    page.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>Images</title></head>
      <body style="margin: 0"><section style="padding: 40px">
        <h2>Team</h2>
        <img src="{svg}" width="40" height="40" alt="Portrait of   the founder">
        <img src="{svg}" width="40" height="40" alt="">
        <img src="{svg}" width="40" height="40">
        <ul><li><h3>Project</h3><img src="{svg}" width="40" height="40" alt="Project screenshot"></li><li>Plain</li></ul>
      </section>
      <section style="padding: 40px; background: rgb(30, 30, 60); color: white"><h2>Next</h2></section>
      </body></html>""")
    out = tmp_path / "run"
    assert capture(page.as_uri(), out, width=1000, height=700).status == "complete"
    assert cli("detect", out).exit_code == 0 and cli("report", out).exit_code == 0
    message = json.loads((out / "report.json").read_text())["sections"][0]["message"]
    svg = {"src": "data:image/svg+xml"}  # embedded images keep only their type, not their bytes
    assert message == [
        {"tag": "h2", "text": "Team"},
        {"tag": "img", "text": "Portrait of the founder", **svg},
        {"tag": "img", **svg},  # alt="": listed by its source
        {"tag": "img", **svg},  # no alt attribute: listed by its source
        {"tag": "ul", "children": [
            {"tag": "li", "children": [{"tag": "h3", "text": "Project"}, {"tag": "img", "text": "Project screenshot", **svg}]},
            {"tag": "li", "text": "Plain"},
        ]},
    ]



def test_report_json_links_are_a_entries_and_carousel_slides_count(tmp_path):
    page = tmp_path / "links.html"
    page.write_text("""<!doctype html><html><head><meta charset="utf-8"><title>Links</title></head>
      <body style="margin: 0; overflow-x: hidden"><section style="padding: 40px">
        <h2>Let us talk</h2>
        <a href="#contact" class="button"><span>Let's</span> <span>talk</span></a>
        <a href="#case"><h3>Case study</h3><p>Read how</p></a>
        <div style="overflow: hidden; width: 600px"><div style="display: flex; transform: translateX(-1200px)">
          <p style="flex: none; width: 600px">Slide one</p><p style="flex: none; width: 600px">Slide two</p>
          <p style="flex: none; width: 600px">Slide three</p></div></div>
      </section>
      <section style="padding: 40px; background: rgb(30, 30, 60); color: white"><h2>Next</h2></section>
      </body></html>""")
    out = tmp_path / "run"
    assert capture(page.as_uri(), out, width=1000, height=700).status == "complete"
    assert cli("detect", out).exit_code == 0 and cli("report", out).exit_code == 0
    text = json.dumps(json.loads((out / "report.json").read_text())["sections"][0]["message"])
    assert '{"tag": "a", "text": "Let\'s talk"}' in text  # one entry for the whole button label
    assert '{"tag": "h3", "text": "Case study"}' in text  # a card link stays a container of its parts
    assert all(f"Slide {n}" in text for n in ("one", "two", "three"))  # slides outside the page area count



def test_image_src_is_the_displayed_url(tmp_path):
    (tmp_path / "photo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg' width='40' height='40'/>")
    page = tmp_path / "page.html"
    page.write_text("""<!doctype html><html><head><meta charset="utf-8"><title>Src</title></head>
      <body style="margin: 0"><section style="padding: 40px"><h2>Photo</h2><img src="photo.svg" width="40" height="40"></section>
      <section style="padding: 40px; background: rgb(30, 30, 60)"><h2>Next</h2></section></body></html>""")
    out = tmp_path / "run"
    assert capture(page.as_uri(), out, width=1000, height=700).status == "complete"
    assert cli("detect", out).exit_code == 0 and cli("report", out).exit_code == 0
    message = json.loads((out / "report.json").read_text())["sections"][0]["message"]
    assert message[1] == {"tag": "img", "src": (tmp_path / "photo.svg").as_uri()}  # resolved to a full URL

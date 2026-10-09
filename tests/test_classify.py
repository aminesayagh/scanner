import json

import httpx2
import pytest
from typer.testing import CliRunner
from typesafe_sdk import RetryPolicy, TypeSafeClient

from section_scan import cli as cli_mod
from section_scan import classify
from section_scan.labels import LABELS, REGISTRY_VERSION

LONG = "Lorem ipsum dolor sit amet. " * 40


def report_doc():
    quote = {"tag": "p", "text": "Amine is an incredible developer."}
    return {
        "link": "https://example.com/", "title": "Example studio", "description": "Software for business.",
        "sections": [
            {"id": None, "position": {}, "flags": [], "role": "header",
             "message": [{"tag": "a", "text": "Home"}, {"tag": "a", "text": "Contact"}]},
            {"id": "hero", "position": {}, "flags": [], "role": "flow",
             "message": [{"tag": "h1", "text": "We build software"}, {"tag": "a", "text": "Let's talk", "hidden": ["mobile"]}]},
            {"id": None, "position": {}, "flags": [], "role": "overlay", "message": []},
            {"id": "people", "position": {}, "flags": ["ambiguous"], "role": "flow",
             "message": [{"tag": "h2", "text": "People story."}, {"tag": "group", "children": [quote, quote, {"tag": "p", "text": "Great work."}]}, quote]},
            {"id": "faq", "position": {}, "flags": [], "role": "flow",
             "message": [{"tag": "h2", "text": "Frequently asked questions"},
                         {"tag": "ul", "children": [{"tag": "li", "text": f"Question {n}? {LONG}"} for n in range(12)]}]},
        ],
    }


class FakeJev:
    """A stand-in TypeSafe API: a keyword rule picks the label; every request is recorded."""

    def __init__(self, fail_on: str | None = None):
        self.requests, self.fail_on = [], fail_on

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        text = json.dumps(body["state"]).lower()
        if self.fail_on and self.fail_on in text:
            return httpx2.Response(500, json={"detail": "upstream failure"}, headers={"x-typesafe-request-id": "req-fail"})
        label = "faq" if "frequently" in text else "testimonials" if "people story" in text else "hero" if "we build" in text else "header"
        labels = list(body["questions"]["section_label"]["criteria"])
        probs = {name: (0.85 if name == label else 0.15 / (len(labels) - 1)) for name in labels}
        answer = {"type": "choice", "choice": label, "confidence": 0.83, "probabilities": probs}
        return httpx2.Response(200, json={"model": "jev-2026-09-15", "usage": {"input_tokens": 321, "output_tokens": 1},
                                          "answers": {"section_label": answer}},
                              headers={"x-typesafe-request-id": f"req-{len(self.requests)}"})


def client_for(api):
    return TypeSafeClient(api_key="test-key", transport=httpx2.MockTransport(api), retry=RetryPolicy(max_retries=0))


@pytest.fixture
def run(tmp_path):
    (tmp_path / "report.json").write_text(json.dumps(report_doc()))
    return tmp_path


def proposals(run, api, model="jev-latest", hide_dom_id=False):
    doc = json.loads((run / "report.json").read_text())
    return classify.ensure_proposals(run, doc, lambda: client_for(api), model, hide_dom_id)


def test_one_jev_request_per_section_with_the_full_registry(run):
    api = FakeJev()
    state = proposals(run, api)
    assert len(api.requests) == 4  # 5 sections; the empty overlay never reaches the API
    question = api.requests[0]["questions"]["section_label"]
    assert question["type"] == "choice" and list(question["criteria"]) == list(LABELS)
    assert "supporting clues" in json.dumps(question["instructions"])
    hero = state.sections["section-1"].proposal
    assert (hero.label, hero.source, hero.model, hero.registry_version) == ("hero", "jev", "jev-2026-09-15", REGISTRY_VERSION)
    assert set(hero.probabilities) == set(LABELS) and hero.confidence == pytest.approx(0.83)


def test_section_without_content_is_unknown_without_a_request(run):
    state = proposals(run, FakeJev())
    overlay = state.sections["section-2"].proposal
    assert (overlay.label, overlay.source, overlay.reason) == ("unknown", "preprocessing", "insufficient_content")


def test_input_keeps_hierarchy_drops_duplicates_and_breakpoint_lists(run):
    api = FakeJev()
    proposals(run, api)
    people = next(r["state"] for r in api.requests if "People story" in json.dumps(r["state"]))
    assert people["section"]["content"] == [
        {"tag": "h2", "text": "People story."},
        {"tag": "group", "children": [{"tag": "p", "text": "Amine is an incredible developer."}, {"tag": "p", "text": "Great work."}]},
        {"tag": "p", "text": "Amine is an incredible developer."},
    ]
    assert people["page"] == {"title": "Example studio", "description": "Software for business."}
    assert (people["section"]["order"], people["section"]["layout_role"], people["section"]["dom_id"]) == (4, "flow", "people")
    assert "hidden" not in json.dumps([r["state"] for r in api.requests])


def test_long_section_is_shortened_and_recorded(run):
    state = proposals(run, FakeJev())
    faq = state.sections["section-4"].proposal
    assert faq.input_shortened
    doc = json.loads((run / "report.json").read_text())
    content, shortened = classify.build_input(doc, 4)
    assert shortened and content["section"]["content"][0] == {"tag": "h2", "text": "Frequently asked questions"}
    assert len(json.dumps(content["section"]["content"])) <= classify.CONTENT_BUDGET


def test_failed_request_is_not_unknown_and_is_retried(run):
    state = proposals(run, FakeJev(fail_on="frequently"))
    faq = state.sections["section-4"].proposal
    assert faq.status == "failed" and faq.label is None and "InternalServerError" in faq.error
    api = FakeJev()
    state = proposals(run, api)
    assert len(api.requests) == 1 and state.sections["section-4"].proposal.label == "faq"


def test_resume_reuses_only_matching_proposals(run):
    proposals(run, FakeJev())
    classify.decide(run, classify.load_state(run), "section-1", "confirmed", "hero")
    classify.decide(run, classify.load_state(run), "section-3", "confirmed", "testimonials")
    api = FakeJev()
    proposals(run, api)
    assert api.requests == []  # nothing changed: no request
    doc = json.loads((run / "report.json").read_text())
    doc["sections"][3]["message"][0]["text"] = "Kind words."
    (run / "report.json").write_text(json.dumps(doc))
    state = proposals(run, api)
    assert len(api.requests) == 1  # only the changed section
    assert state.sections["section-3"].decision is None  # its decision was made on other evidence
    assert state.sections["section-1"].decision.label == "hero"
    api = FakeJev()
    proposals(run, api, model="jev-2")
    assert len(api.requests) == 4  # another model: every proposal is renewed


def test_hide_dom_id_removes_the_hint(run):
    api = FakeJev()
    proposals(run, api, hide_dom_id=True)
    assert all(r["state"]["section"]["dom_id"] is None for r in api.requests)


def test_cli_review_saves_each_decision_and_exports(run, monkeypatch):
    monkeypatch.setattr(cli_mod, "make_typesafe_client", lambda: client_for(FakeJev()))
    # header: accept; hero: choose cta; overlay: unresolved; then quit before people and faq.
    result = CliRunner().invoke(cli_mod.app, ["classify", str(run)], input="a\nc\ncta\nu\nq\n")
    assert result.exit_code == 0, result.output
    state = classify.load_state(run)
    decisions = {k: (s.decision.status, s.decision.label) for k, s in state.sections.items() if s.decision}
    assert decisions == {"section-0": ("confirmed", "header"), "section-1": ("confirmed", "cta"), "section-2": ("unresolved", None)}
    doc = json.loads((run / "report-classified.json").read_text())
    original = report_doc()
    for got, want in zip(doc["sections"], original["sections"]):
        assert {k: got[k] for k in want} == want  # original data kept
    hero = doc["sections"][1]["classification"]
    assert hero["proposed_label"] == "hero" and hero["confirmed_label"] == "cta" and hero["status"] == "confirmed"
    assert hero["source"] == "jev" and hero["model"] == "jev-2026-09-15" and hero["registry_version"] == REGISTRY_VERSION
    assert set(hero["probabilities"]) == set(LABELS) and "confidence" in hero
    overlay = doc["sections"][2]["classification"]
    assert overlay["status"] == "unresolved" and overlay["source"] == "preprocessing" and overlay["reason"] == "insufficient_content"
    assert doc["sections"][3]["classification"]["status"] == "pending"
    # Resume: the next session starts at the first undecided section.
    result = CliRunner().invoke(cli_mod.app, ["classify", str(run)], input="a\na\n")
    assert result.exit_code == 0 and "section-0 ·" not in result.output and "section-3 ·" in result.output
    assert classify.load_state(run).sections["section-4"].decision.label == "faq"


def test_missing_api_key_is_a_clear_error(run, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.chdir(run)  # away from the project's .env, which may hold a real key
    result = CliRunner().invoke(cli_mod.app, ["classify", str(run), "--no-review"])
    assert result.exit_code == 2 and "TYPESAFE_API_KEY" in result.output


def test_dotenv_provides_the_key_without_overriding_the_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nTYPESAFE_API_KEY='from-file'\nEMPTY=\n")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cli_mod.load_dotenv(env)
    assert cli_mod.os.environ["TYPESAFE_API_KEY"] == "from-file" and "EMPTY" not in cli_mod.os.environ
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-shell")
    cli_mod.load_dotenv(env)
    assert cli_mod.os.environ["TYPESAFE_API_KEY"] == "from-shell"

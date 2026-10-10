"""Section classification with Jev (TypeSafe): prepare inputs, propose labels, record decisions.

The source report.json is never changed. State lives in `classification.json` next to it (saved after every
proposal and decision); `report-classified.json` is the enriched copy.
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from urllib.parse import urlparse

from pydantic import BaseModel

from .labels import (
    INDUSTRIES,
    INDUSTRY_INSTRUCTIONS,
    INSTRUCTIONS,
    INSTRUCTIONS_VERSION,
    LABELS,
    PAGE_INSTRUCTIONS_VERSION,
    PAGE_REGISTRY_VERSION,
    PAGE_SCOPES,
    REGISTRY_VERSION,
    SCOPE_INSTRUCTIONS,
)

# Page-level questions: name -> (instructions, registry). Asked together in one request.
PAGE_QUESTIONS = {"industry": (INDUSTRY_INSTRUCTIONS, INDUSTRIES), "scope": (SCOPE_INSTRUCTIONS, PAGE_SCOPES)}
PAGE_BUDGET = 8000  # serialized page input before excerpts are trimmed

QUESTION = "section_label"
# Serialized size of a section's content before it is shortened (the API does not document a budget).
# ponytail: a fixed character budget; switch to measured token usage once real requests show the limits.
CONTENT_BUDGET = 6000
REPRESENTATIVE_ITEMS = 3  # kept per group when shortening
MIN_EVIDENCE_CHARS = 3  # letters or digits needed before a section is worth classifying


class Proposal(BaseModel):
    key: str  # fingerprint of everything the proposal depends on
    status: Literal["proposed", "failed"]
    source: Literal["jev", "preprocessing"]
    label: str | None
    probabilities: dict[str, float] | None = None
    confidence: float | None = None
    model: str | None = None  # the model that answered (from the response)
    requested_model: str | None = None
    registry_version: str = REGISTRY_VERSION
    instructions_version: str = INSTRUCTIONS_VERSION
    reason: str | None = None  # preprocessing: why no request was made
    error: str | None = None  # failed: what went wrong (classification did not complete)
    request_id: str | None = None
    input_tokens: int | None = None
    input_shortened: bool = False
    at: datetime


class Decision(BaseModel):
    proposal_key: str  # the proposal this decision was made on
    status: Literal["confirmed", "unresolved"]
    label: str | None
    at: datetime


class SectionState(BaseModel):
    proposal: Proposal | None = None
    decision: Decision | None = None


class Answer(BaseModel):
    label: str
    probabilities: dict[str, float] | None = None
    confidence: float | None = None


class PageProposal(BaseModel):
    """Industry and scope (page type), proposed together from one request."""

    key: str
    status: Literal["proposed", "failed"]
    source: Literal["jev", "preprocessing"]
    answers: dict[str, Answer] = {}  # "industry", "scope"
    model: str | None = None
    requested_model: str | None = None
    registry_version: str = PAGE_REGISTRY_VERSION
    instructions_version: str = PAGE_INSTRUCTIONS_VERSION
    reason: str | None = None
    error: str | None = None
    request_id: str | None = None
    input_tokens: int | None = None
    input_shortened: bool = False
    at: datetime


class PageState(BaseModel):
    proposal: PageProposal | None = None
    decisions: dict[str, Decision] = {}  # question -> decision


class ClassificationState(BaseModel):
    report_sha: str
    sections: dict[str, SectionState] = {}  # section key -> state
    page: PageState = PageState()


# --- input ----------------------------------------------------------------------------------------------


def section_key(index: int) -> str:
    return f"section-{index}"


def _strip(entry):
    """Content only: tag, text and children, without per-breakpoint `hidden` lists."""
    out = {"tag": entry["tag"]}
    if "text" in entry:
        out["text"] = entry["text"]
    if "children" in entry:
        out["children"] = [_strip(c) for c in entry["children"]]
    return out


def _dedupe(entries: list) -> list:
    """Drop exact repeats (a carousel repeating its testimonials), keeping the first, at every level."""
    seen, out = set(), []
    for e in entries:
        if "children" in e:
            e = {**e, "children": _dedupe(e["children"])}
        sig = json.dumps(e, sort_keys=True, ensure_ascii=False)
        if sig not in seen:
            seen.add(sig)
            out.append(e)
    return out


def _shorten(entries: list) -> list:
    """Headings, plus the first items of every group."""
    out = []
    for e in entries:
        if "children" in e:
            out.append({**e, "children": _shorten(e["children"])[:REPRESENTATIVE_ITEMS]})
        else:
            out.append(e)
    headings = [e for e in out if e["tag"] in ("h1", "h2", "h3", "h4", "h5", "h6")]
    rest = [e for e in out if e not in headings]
    while rest and len(json.dumps(headings + rest, ensure_ascii=False)) > CONTENT_BUDGET:
        rest.pop()
    return [e for e in out if e in headings or e in rest]


def build_input(report: dict, index: int, hide_dom_id: bool = False) -> tuple[dict, bool]:
    """The classification state for one section, and whether its content had to be shortened."""
    section = report["sections"][index]
    content = _dedupe([_strip(e) for e in section["message"]])
    shortened = len(json.dumps(content, ensure_ascii=False)) > CONTENT_BUDGET
    if shortened:
        content = _shorten(content)
    state = {
        "page": {"title": report.get("title"), "description": report.get("description")},
        "section": {
            "order": index + 1,
            "of": len(report["sections"]),
            "layout_role": section["role"],
            "dom_id": None if hide_dom_id else section.get("id"),
            "content": content,
        },
    }
    return state, shortened


def has_evidence(state: dict) -> bool:
    """Enough letters or digits in the section's own text (counted on the strings, not on JSON)."""
    return sum(ch.isalnum() for t in _texts(state["section"]["content"]) for ch in t) >= MIN_EVIDENCE_CHARS


def _texts(entries) -> list[str]:
    return [t for e in entries for t in [e.get("text", "")] + _texts(e.get("children", []))]


def fingerprint(state: dict, requested_model: str) -> str:
    """Everything a proposal depends on: input, instructions, registry and model."""
    payload = json.dumps(
        {"state": state, "instructions": INSTRUCTIONS, "labels": LABELS, "registry": REGISTRY_VERSION, "model": requested_model},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --- proposals ------------------------------------------------------------------------------------------


def requested_model() -> str:
    return os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip() or "jev-latest"


def propose(state: dict, shortened: bool, key: str, client, model: str) -> Proposal:
    """One Jev request; sections without evidence never reach the API."""
    now = datetime.now(UTC)
    if not has_evidence(state):
        return Proposal(
            key=key, status="proposed", source="preprocessing", label="unknown",
            reason="insufficient_content", input_shortened=shortened, at=now,
        )
    from typesafe_sdk import Choice, TypeSafeError  # imported here: only needed when calling the API

    try:
        response = client.system_one(state, {QUESTION: Choice(instructions=INSTRUCTIONS, criteria=LABELS)}, model=model)
        answer = response.choices[QUESTION]
    except TypeSafeError as e:
        return Proposal(
            key=key, status="failed", source="jev", label=None, requested_model=model,
            error=f"{type(e).__name__}: {e}", input_shortened=shortened, at=now,
        )
    return Proposal(
        key=key, status="proposed", source="jev", label=answer.choice,
        probabilities=dict(answer.probabilities), confidence=answer.confidence,
        model=response.model, requested_model=model, request_id=response.request_id,
        input_tokens=response.usage.input_tokens, input_shortened=shortened, at=now,
    )


def report_sha(report_path: Path) -> str:
    return hashlib.sha256(report_path.read_bytes()).hexdigest()[:16]


def load_state(run: Path) -> ClassificationState | None:
    path = run / "classification.json"
    return ClassificationState.model_validate_json(path.read_text()) if path.exists() else None


def save_state(run: Path, state: ClassificationState) -> None:
    tmp = run / "classification.json.tmp"
    tmp.write_text(state.model_dump_json(indent=1))
    os.replace(tmp, run / "classification.json")  # atomic: progress is never half-written


def ensure_proposals(run: Path, report: dict, client_factory, model: str, hide_dom_id: bool, on_progress=None):
    """Propose a label for every section, reusing stored proposals whose fingerprint still matches.

    Failed proposals are retried. The client is only created when a request is actually needed.
    """
    state = load_state(run) or ClassificationState(report_sha=report_sha(run / "report.json"))
    state.report_sha = report_sha(run / "report.json")
    client = None
    for i in range(len(report["sections"])):
        key_name = section_key(i)
        section_input, shortened = build_input(report, i, hide_dom_id)
        key = fingerprint(section_input, model)
        current = state.sections.get(key_name, SectionState())
        if current.proposal and current.proposal.key == key and current.proposal.status == "proposed":
            continue
        if client is None and has_evidence(section_input):
            client = client_factory()
        current = SectionState(
            proposal=propose(section_input, shortened, key, client, model),
            # A decision made on another proposal does not carry over: the evidence changed.
            decision=current.decision if current.decision and current.decision.proposal_key == key else None,
        )
        state.sections[key_name] = current
        save_state(run, state)
        if on_progress:
            on_progress(key_name, current.proposal)
    return state


# --- page: industry and scope -----------------------------------------------------------------------------


def section_label(state: ClassificationState, index: int) -> str | None:
    """The best current label of a section: the confirmed one, else Jev's or preprocessing's proposal."""
    s = state.sections.get(section_key(index))
    if s is None:
        return None
    if s.decision and s.decision.status == "confirmed":
        return s.decision.label
    return s.proposal.label if s.proposal and s.proposal.status == "proposed" else None


def build_page_input(report: dict, state: ClassificationState) -> tuple[dict, bool]:
    sections = []
    for i, section in enumerate(report["sections"]):
        texts = []

        def walk(entries):
            for e in entries:
                if "text" in e:
                    texts.append(e)
                walk(e.get("children", []))

        walk(section["message"])
        headings = list(dict.fromkeys(e["text"] for e in texts if e["tag"] in ("h1", "h2", "h3")))[:3]
        excerpt = " · ".join(dict.fromkeys(e["text"] for e in texts if e["text"] not in headings))
        sections.append({
            "order": i + 1, "layout_role": section["role"], "label": section_label(state, i),
            "dom_id": section.get("id"), "headings": headings, "excerpt": excerpt[:300],
        })
    page = {
        "page": {"domain": urlparse(report.get("link") or "").netloc or None,
                 "title": report.get("title"), "description": report.get("description")},
        "sections": sections,
    }
    shortened = False
    for limit in (150, 60, 0):  # trim excerpts until the input fits
        if len(json.dumps(page, ensure_ascii=False)) <= PAGE_BUDGET:
            break
        shortened = True
        for sec in sections:
            sec["excerpt"] = sec["excerpt"][:limit]
    return page, shortened


def page_has_evidence(page: dict) -> bool:
    parts = [page["page"]["title"] or "", page["page"]["description"] or ""]
    parts += [t for sec in page["sections"] for t in sec["headings"] + [sec["excerpt"]]]
    return sum(ch.isalnum() for t in parts for ch in t) >= MIN_EVIDENCE_CHARS


def page_fingerprint(page: dict, requested_model: str) -> str:
    payload = json.dumps(
        {"state": page, "questions": PAGE_QUESTIONS, "registry": PAGE_REGISTRY_VERSION, "model": requested_model},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def propose_page(page: dict, shortened: bool, key: str, client, model: str) -> PageProposal:
    now = datetime.now(UTC)
    if not page_has_evidence(page):
        return PageProposal(
            key=key, status="proposed", source="preprocessing",
            answers={q: Answer(label="unknown") for q in PAGE_QUESTIONS},
            reason="insufficient_content", input_shortened=shortened, at=now,
        )
    from typesafe_sdk import Choice, TypeSafeError

    questions = {q: Choice(instructions=instr, criteria=registry) for q, (instr, registry) in PAGE_QUESTIONS.items()}
    try:
        response = client.system_one(page, questions, model=model)
        answers = {
            q: Answer(label=a.choice, probabilities=dict(a.probabilities), confidence=a.confidence)
            for q, a in ((q, response.choices[q]) for q in PAGE_QUESTIONS)
        }
    except TypeSafeError as e:
        return PageProposal(
            key=key, status="failed", source="jev", requested_model=model,
            error=f"{type(e).__name__}: {e}", input_shortened=shortened, at=now,
        )
    return PageProposal(
        key=key, status="proposed", source="jev", answers=answers, model=response.model,
        requested_model=model, request_id=response.request_id, input_tokens=response.usage.input_tokens,
        input_shortened=shortened, at=now,
    )


def ensure_page_proposal(run: Path, report: dict, state: ClassificationState, client_factory, model: str) -> PageProposal:
    """Propose industry and scope from the page and its current section labels (reused when unchanged)."""
    page, shortened = build_page_input(report, state)
    key = page_fingerprint(page, model)
    current = state.page
    if current.proposal and current.proposal.key == key and current.proposal.status == "proposed":
        return current.proposal
    client = client_factory() if page_has_evidence(page) else None
    proposal = propose_page(page, shortened, key, client, model)
    kept = {q: d for q, d in current.decisions.items() if d.proposal_key == key}
    state.page = PageState(proposal=proposal, decisions=kept)
    save_state(run, state)
    return proposal


def decide_page(run: Path, state: ClassificationState, question: str, status: str, label: str | None) -> None:
    state.page.decisions[question] = Decision(
        proposal_key=state.page.proposal.key, status=status, label=label, at=datetime.now(UTC)
    )
    save_state(run, state)


# --- decisions and export ---------------------------------------------------------------------------------


def decide(run: Path, state: ClassificationState, key_name: str, status: str, label: str | None) -> None:
    proposal = state.sections[key_name].proposal
    state.sections[key_name].decision = Decision(
        proposal_key=proposal.key, status=status, label=label, at=datetime.now(UTC)
    )
    save_state(run, state)


def export(run: Path, report: dict, state: ClassificationState) -> Path:
    """report-classified.json: the original report, each section with a `classification` object."""
    enriched = json.loads(json.dumps(report))
    for i, section in enumerate(enriched["sections"]):
        s = state.sections.get(section_key(i), SectionState())
        p, d = s.proposal, s.decision
        if p is None:
            c = {"status": "pending"}
        elif p.status == "failed":
            c = {"status": "failed", "source": p.source, "error": p.error, "model": p.requested_model}
        else:
            c = {
                "proposed_label": p.label,
                "confirmed_label": d.label if d and d.status == "confirmed" else None,
                "status": d.status if d else "pending",
                "source": p.source,
                "model": p.model,
                "registry_version": p.registry_version,
            }
            if p.source == "jev":
                c |= {"probabilities": p.probabilities, "confidence": p.confidence}
            if p.reason:
                c["reason"] = p.reason
            if p.input_shortened:
                c["input_shortened"] = True
        section["key"] = section_key(i)
        section["classification"] = c
    enriched["classification"] = _page_export(state.page)
    out = run / "report-classified.json"
    out.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))
    return out


def _page_export(page: PageState) -> dict:
    p = page.proposal
    out = {}
    for q in PAGE_QUESTIONS:
        if p is None:
            out[q] = {"status": "pending"}
            continue
        if p.status == "failed":
            out[q] = {"status": "failed", "source": p.source, "error": p.error, "model": p.requested_model}
            continue
        a, d = p.answers[q], page.decisions.get(q)
        c = {
            "proposed_label": a.label,
            "confirmed_label": d.label if d and d.status == "confirmed" else None,
            "status": d.status if d else "pending",
            "source": p.source,
            "model": p.model,
            "registry_version": p.registry_version,
        }
        if p.source == "jev":
            c |= {"probabilities": a.probabilities, "confidence": a.confidence}
        if p.reason:
            c["reason"] = p.reason
        if p.input_shortened:
            c["input_shortened"] = True
        out[q] = c
    return out

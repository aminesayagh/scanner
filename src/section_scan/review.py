"""Review state: corrections and confirmations on top of the automatic proposals (milestone 6).

Operations are pure: each returns a new ReviewState or raises ReviewError, so an invalid edit never
leaves a half-applied change. Persistence writes a temporary file and renames it into place; undo
restores whole-state snapshots kept in review-history.json.
"""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from .models import Element, ElementSnapshot, ProposalSnapshot, Rect, ReviewOperation, ReviewRegion, ReviewState

_HISTORY = TypeAdapter(list[ReviewState])


class ReviewError(ValueError):
    """An edit that cannot be applied; the current state stays as it was."""


# --- persistence ----------------------------------------------------------------------------------


def fingerprint(state: ReviewState) -> str:
    """Identifies exactly what a report shows: the proposals and every region as reviewed.

    Revision numbers repeat after undo or --restart; this does not, unless the content is identical.
    """
    content = state.proposal_revision + "".join(r.model_dump_json() for r in state.regions)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def proposal_revision(run: Path) -> str:
    return hashlib.sha256((run / "proposals.json").read_bytes()).hexdigest()[:12]


def start(run: Path) -> ReviewState:
    props = ProposalSnapshot.model_validate_json((run / "proposals.json").read_text())
    regions = [
        ReviewRegion(
            id=s.id,
            label=s.label,
            role=s.role,
            status="proposed",
            bounds=s.bounds,
            element_ids=s.element_ids,
            source_section_ids=[s.id],
            ambiguous=s.ambiguous,
            notes=[],
            limitations=s.limitations,
        )
        for s in props.sections
    ]
    return ReviewState(
        capture_id=props.capture_id,
        proposal_revision=proposal_revision(run),
        revision=0,
        next_region_number=1,
        regions=regions,
        corrections=[],
        confirmation_revision=None,
    )


def load(run: Path) -> ReviewState | None:
    path = run / "review.json"
    return ReviewState.model_validate_json(path.read_text()) if path.exists() else None


def history(run: Path) -> list[ReviewState]:
    path = run / "review-history.json"
    return _HISTORY.validate_json(path.read_text()) if path.exists() else []


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # atomic on the same filesystem: readers see the old or the new file


def save(run: Path, new: ReviewState, previous: ReviewState | None) -> None:
    """Persist `new`, keeping `previous` for undo. History first: a crash can only leave an extra snapshot."""
    if previous is not None:
        _write_atomic(run / "review-history.json", _HISTORY.dump_json(history(run) + [previous], indent=1))
    _write_atomic(run / "review.json", new.model_dump_json(indent=1).encode())


def undo(run: Path) -> ReviewState:
    past = history(run)
    if not past:
        raise ReviewError("nothing to undo")
    _write_atomic(run / "review.json", past[-1].model_dump_json(indent=1).encode())
    _write_atomic(run / "review-history.json", _HISTORY.dump_json(past[:-1], indent=1))
    return past[-1]


def archive(run: Path, state: ReviewState) -> Path:
    """Move a review aside (e.g. before reviewing a new detection run); nothing is deleted."""
    dest = run / f"review-{state.proposal_revision}-r{state.revision}.json"
    _write_atomic(dest, state.model_dump_json(indent=1).encode())
    for name in ("review.json", "review-history.json"):
        (run / name).unlink(missing_ok=True)
    return dest


# --- operations -----------------------------------------------------------------------------------


def _next(state: ReviewState, command: str, regions: list[ReviewRegion], **changes) -> ReviewState:
    revision = state.revision + 1
    data = state.model_dump()
    data.update(
        revision=revision,
        regions=[r.model_dump() for r in regions],
        corrections=data["corrections"] + [{"revision": revision, "command": command, "at": datetime.now(UTC)}],
        **changes,
    )
    return ReviewState.model_validate(data)  # re-validates: unique IDs, no element in two regions


def _find(state: ReviewState, rid: str) -> tuple[int, ReviewRegion]:
    for i, r in enumerate(state.regions):
        if r.id == rid:
            return i, r
    raise ReviewError(f"no region {rid!r}; known: {', '.join(r.id for r in state.regions)}")


def _changed(r: ReviewRegion, **changes) -> ReviewRegion:
    # Geometry or membership changes invalidate confirmation for the affected region.
    return r.model_copy(update={"status": "proposed", **changes})


def rename(state: ReviewState, rid: str, label: str) -> ReviewState:
    if not label.strip():
        raise ReviewError("label must not be empty")
    i, r = _find(state, rid)
    regions = list(state.regions)
    regions[i] = r.model_copy(update={"label": label.strip()})  # naming is not a geometry change
    return _next(state, f"rename {rid} {label!r}", regions)


def merge(state: ReviewState, first: str, second: str) -> ReviewState:
    i, a = _find(state, first)
    j, b = _find(state, second)
    if abs(i - j) != 1:
        raise ReviewError(f"{first} and {second} are not adjacent")
    if a.role != "flow" or b.role != "flow":
        raise ReviewError("only flow sections can be merged; header, footer and overlays stay separate")
    if "excluded" in (a.status, b.status):
        raise ReviewError("cannot merge an excluded region; undo the exclusion first")
    if i > j:
        (i, a), (j, b) = (j, b), (i, a)
    rid = f"n{state.next_region_number}"
    merged = ReviewRegion(
        id=rid,
        label=a.label,
        role="flow",
        status="proposed",
        bounds=_union(a.bounds, b.bounds),
        element_ids=a.element_ids + b.element_ids,
        source_section_ids=a.source_section_ids + b.source_section_ids,
        ambiguous=False,
        notes=a.notes + b.notes + [f"merged from {a.id} and {b.id}"],
        limitations=a.limitations + b.limitations,
    )
    regions = state.regions[:i] + [merged] + state.regions[j + 1 :]
    return _next(
        state, f"merge {first} {second}", regions, next_region_number=state.next_region_number + 1,
        confirmation_revision=None,
    )


def split(
    state: ReviewState, rid: str, y: float, elements: ElementSnapshot, crossing: str | None = None, at: str = ""
) -> ReviewState:
    """Cut a flow region at document y. Members wholly above or below go with that side; wrappers that
    span the cut go to the upper part (they start there); content that spans the cut needs `crossing`
    ("above" or "below"), an explicit allocation rather than silent duplication or deletion.
    """
    i, r = _find(state, rid)
    if r.role != "flow":
        raise ReviewError("split is supported for flow sections only")
    top, bottom = r.bounds.y, r.bounds.y + r.bounds.height
    if not top < y < bottom:
        raise ReviewError(f"y={y:g} is outside {rid} ({top:g}–{bottom:g})")
    by_id = {e.id: e for e in elements.elements}
    members = set(r.element_ids)
    upper, lower, wrappers, spanning = [], [], [], []
    for eid in r.element_ids:
        b = by_id[eid].bounds
        if b.y + b.height <= y:
            upper.append(eid)
        elif b.y >= y:
            lower.append(eid)
        elif _is_wrapper(by_id[eid], members):
            wrappers.append(eid)
        else:
            spanning.append(eid)
    if spanning and crossing not in ("above", "below"):
        listed = ", ".join(spanning[:5]) + (" …" if len(spanning) > 5 else "")
        raise ReviewError(
            f"{len(spanning)} content element(s) span y={y:g} ({listed}); choose where they go with "
            "crossing=above or crossing=below"
        )
    (upper if crossing == "above" else lower).extend(spanning)
    upper += wrappers
    if not _has_content(upper, by_id, members) or not _has_content(lower, by_id, members):
        raise ReviewError(f"splitting {rid} at y={y:g} would leave one part without content")

    n = state.next_region_number
    where = f"candidate boundary {at}" if at else "a manual boundary (no DOM boundary here)"
    note = f"split from {rid} at y={y:g}, {where}"
    alloc = []
    if wrappers:
        alloc.append(f"wrappers spanning the cut allocated to the upper part: {', '.join(wrappers)}")
    if spanning:
        alloc.append(f"content spanning the cut allocated {crossing}: {', '.join(spanning)}")
    # The part that receives spanning content extends to contain it, so bounds always hold the members.
    upper_bottom, lower_top = y, y
    if spanning and crossing == "above":
        upper_bottom = max(y, *(by_id[e].bounds.y + by_id[e].bounds.height for e in spanning))
    if spanning and crossing == "below":
        lower_top = min(y, *(by_id[e].bounds.y for e in spanning))
    parts = [
        _changed(
            r, id=f"n{n}", label=f"{r.label} (upper)", element_ids=_ordered(upper, r),
            bounds=Rect(x=r.bounds.x, y=top, width=r.bounds.width, height=upper_bottom - top),
            notes=r.notes + [note, *alloc], limitations=[x for x in r.limitations if x.top < upper_bottom],
        ),
        _changed(
            r, id=f"n{n + 1}", label=f"{r.label} (lower)", element_ids=_ordered(lower, r),
            bounds=Rect(x=r.bounds.x, y=lower_top, width=r.bounds.width, height=bottom - lower_top),
            notes=r.notes + [note], limitations=[x for x in r.limitations if x.bottom > lower_top],
        ),
    ]
    regions = state.regions[:i] + parts + state.regions[i + 1 :]
    command = f"split {rid} {at or f'{y:g}'}" + (f" crossing={crossing}" if crossing else "")
    return _next(state, command, regions, next_region_number=n + 2, confirmation_revision=None)


def adjust(
    state: ReviewState, rid: str, elements: ElementSnapshot, top: float | None = None, bottom: float | None = None
) -> ReviewState:
    """Move a flow region's top/bottom edge. Membership is unchanged, so the edges may only trim empty
    space: they must keep every member's content inside and must not run into a neighbouring section.
    """
    i, r = _find(state, rid)
    if r.role != "flow":
        raise ReviewError("adjust is supported for flow sections only")
    old_top, old_bottom = r.bounds.y, r.bounds.y + r.bounds.height
    new_top = old_top if top is None else top
    new_bottom = old_bottom if bottom is None else bottom
    if (new_top, new_bottom) == (old_top, old_bottom):
        raise ReviewError("the edges are unchanged")
    if new_top >= new_bottom:
        raise ReviewError("top must be above bottom")
    by_id = {e.id: e for e in elements.elements}
    members = set(r.element_ids)
    content = [by_id[e].bounds for e in r.element_ids if not _is_wrapper(by_id[e], members)]
    # Only an edge that moves inward is checked against the content it would cut.
    cuts_top = new_top > old_top and content and new_top > min(b.y for b in content)
    cuts_bottom = new_bottom < old_bottom and content and new_bottom < max(b.y + b.height for b in content)
    if cuts_top or cuts_bottom:
        raise ReviewError("the new edges would cut through the section's content; use split instead")
    for j, other in enumerate(state.regions):
        if other.role != "flow" or j == i:
            continue
        o_top, o_bottom = other.bounds.y, other.bounds.y + other.bounds.height
        if new_top < o_bottom and o_top < new_bottom and not (r.bounds.y < o_bottom and o_top < r.bounds.y + r.bounds.height):
            raise ReviewError(f"the new edges would overlap {other.id}")
    regions = list(state.regions)
    regions[i] = _changed(
        r, bounds=Rect(x=r.bounds.x, y=new_top, width=r.bounds.width, height=new_bottom - new_top),
        notes=r.notes + [f"edges adjusted to {new_top:g}–{new_bottom:g}"],
    )
    return _next(state, f"adjust {rid} top={new_top:g} bottom={new_bottom:g}", regions, confirmation_revision=None)


def exclude(state: ReviewState, rid: str) -> ReviewState:
    i, r = _find(state, rid)
    regions = list(state.regions)
    regions[i] = r.model_copy(update={"status": "excluded"})
    return _next(state, f"exclude {rid}", regions, confirmation_revision=None)


def confirm(state: ReviewState, rid: str) -> ReviewState:
    i, r = _find(state, rid)
    if r.status == "excluded":
        raise ReviewError(f"{rid} is excluded; undo the exclusion to confirm it")
    regions = list(state.regions)
    regions[i] = r.model_copy(update={"status": "confirmed"})
    return _next(state, f"confirm {rid}", regions)


def confirm_all(state: ReviewState, report_fingerprint: str | None) -> ReviewState:
    """Confirm every retained region at once: only if the latest report shows exactly this state."""
    if report_fingerprint != fingerprint(state):
        raise ReviewError(
            "regenerate and inspect the report first (`report`), so you confirm what you have seen: "
            "the current report does not show this exact review state"
        )
    regions = [r if r.status == "excluded" else r.model_copy(update={"status": "confirmed"}) for r in state.regions]
    return _next(state, "confirm all", regions, confirmation_revision=state.revision + 1)


# --- helpers --------------------------------------------------------------------------------------


def _is_wrapper(e: Element, members: set[str]) -> bool:
    """A member that contains other members: structure, not content of its own."""
    return any(c in members for c in e.child_ids) and not e.text


def _has_content(ids: list[str], by_id: dict[str, Element], members: set[str]) -> bool:
    return any(not _is_wrapper(by_id[e], members) for e in ids)


def _ordered(ids: list[str], r: ReviewRegion) -> list[str]:
    keep = set(ids)
    return [e for e in r.element_ids if e in keep]


def _union(a: Rect, b: Rect) -> Rect:
    left, top = min(a.x, b.x), min(a.y, b.y)
    right, bottom = max(a.x + a.width, b.x + b.width), max(a.y + a.height, b.y + b.height)
    return Rect(x=left, y=top, width=right - left, height=bottom - top)

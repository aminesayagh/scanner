"""Three-breakpoint captures (laptop, tablet, mobile) and their merged report.json.

Each breakpoint is a separate page load, as a real device would load it, stored as a normal run directory
(`<site>/laptop`, `<site>/tablet`, `<site>/mobile`), so every other command works on it unchanged.

Separate loads can show different random content (a rotating testimonial, a shuffled gallery). Content
is matched across loads by tag and text first (the same link in a desktop nav and a mobile menu, the same
image in a gallery reshuffled per load), then by DOM slot (`Element.path`): content in the same slot is
the same content even when its text differs, so random content is listed once instead of once per
breakpoint. Whatever is left exists only at some breakpoints, and is marked hidden at the others.
"""

from dataclasses import dataclass, field
from pathlib import Path

from . import review as review_mod
from .models import (
    ElementSnapshot,
    Manifest,
    MessageGroup,
    MessageItem,
    MessageListItem,
    ProposalSnapshot,
    Rect,
    ReportPosition,
    SiteReportDocument,
    SiteSection,
    Viewport,
)
from .report import (
    _anchor,
    _content_ids,
    _message,
    _overlap_kinds,
    _with_unclaimed_descendants,
    section_rows,
)


@dataclass(frozen=True)
class Breakpoint:
    width: int
    height: int
    device: str | None  # Playwright device whose user agent, mobile mode and touch are emulated


BREAKPOINTS = {
    "laptop": Breakpoint(1440, 900, None),
    "tablet": Breakpoint(768, 1024, "iPad Mini"),
    "mobile": Breakpoint(390, 844, "iPhone 13"),
}
ORDER = list(BREAKPOINTS)


def site_runs(path: Path) -> dict[str, Path] | None:
    """The breakpoint runs of a multi-breakpoint capture directory, or None for a single run."""
    if (path / "manifest.json").exists():
        return None
    runs = {bp: path / bp for bp in ORDER if (path / bp / "manifest.json").exists()}
    return runs or None


# --- merge ---------------------------------------------------------------------------------------------


@dataclass(eq=False)  # identity, not field equality
class _Unit:
    """One piece of content, wherever it shows: breakpoint -> source element."""

    present: dict[str, str] = field(default_factory=dict)
    tag: str = ""
    text: str = ""


@dataclass(eq=False)  # identity, not field equality
class _Section:
    """One merged section: breakpoint -> source section id."""

    present: dict[str, str] = field(default_factory=dict)
    base: str = ""  # breakpoint whose section gives role, flags and id
    entries: list = field(default_factory=list)  # merged message: _Node or _Group


@dataclass(eq=False)  # identity, not field equality
class _Node:
    unit: _Unit
    children: list["_Node"] | None = None  # a list item with parts of its own


@dataclass(eq=False)  # identity, not field equality
class _Group:
    tag: str
    children: list[_Node]


@dataclass(eq=False)  # identity, not field equality
class _Run:
    bp: str
    manifest: Manifest
    by_id: dict
    rows: list  # retained section rows, top to bottom
    members: dict[str, list[str]]
    messages: dict[str, list]  # section id -> message entries (with source elements)


def merge(runs: dict[str, Path]) -> SiteReportDocument:
    loaded = [_load(bp, path) for bp, path in runs.items()]
    units: dict[tuple[str, str], _Unit] = {}  # (breakpoint, element id) -> unit
    all_units: list[_Unit] = []

    # 1. Content identity across loads: same tag and text, then same DOM slot, else new. Text first, so a
    # gallery that shuffles its items and rebuilds its rows per width still pairs each item with itself;
    # content whose text changes per load (a rotating quote) finds no text match and pairs by slot.
    path_of = {}
    for run in loaded:
        leaves = [leaf for sid in run.messages for leaf in _leaves(run.messages[sid])]
        pending = []
        for tag, text, eid in leaves:
            u = next((x for x in all_units if run.bp not in x.present and x.tag == tag and x.text == text), None)
            if u is None:
                pending.append((tag, text, eid))
            else:
                u.present[run.bp] = eid
        by_path = {}
        for u in all_units:
            if run.bp not in u.present:
                for p in (path_of.get((bp, eid)) for bp, eid in u.present.items()):
                    if p:
                        by_path.setdefault(p, u)
        for tag, text, eid in pending:
            path = run.by_id[eid].path
            u = by_path.pop(path, None) if path else None
            if u is None or run.bp in u.present:
                u = _Unit(tag=tag, text=text)
                all_units.append(u)
            u.present[run.bp] = eid
        for tag, text, eid in leaves:
            path_of[(run.bp, eid)] = run.by_id[eid].path
        for u in all_units:
            if run.bp in u.present:
                units[(run.bp, u.present[run.bp])] = u

    # 2. Sections: the widest breakpoint's list, then sections that exist only at smaller ones. A section
    # matches the merged section holding most of its content; one without content (an image band with no
    # alt text) matches by the DOM paths of its elements. A breakpoint that splits a section in two
    # maps both parts to the same merged section.
    merged: list[_Section] = []
    unit_section: dict[int, _Section] = {}
    paths = {(r.bp, rid): {r.by_id[e].path for e in r.members[rid] if r.by_id[e].path} for r in loaded for rid, *_ in r.rows}
    for run in loaded:
        previous = None
        for rid, *_ in run.rows:
            own = [units[(run.bp, eid)] for _, _, eid in _leaves(run.messages[rid])]
            votes = {id(m): 0 for m in merged}
            for u in own:
                if id(u) in unit_section:
                    votes[id(unit_section[id(u)])] += 1
            if not any(votes.values()):
                mine = paths[(run.bp, rid)]
                for m in merged:
                    votes[id(m)] = max(len(mine & paths[(bp, sid)]) for bp, sid in m.present.items())
            best = max(merged, key=lambda m: votes[id(m)], default=None)
            target = best if best is not None and votes[id(best)] > 0 else None
            if target is None:
                target = _Section(base=run.bp)
                merged.insert(merged.index(previous) + 1 if previous is not None else 0, target)
            target.present.setdefault(run.bp, rid)
            for u in own:
                unit_section.setdefault(id(u), target)
            _merge_entries(target.entries, _nodes(run.messages[rid], run.bp, units), placed=_placed(merged, target))
            previous = target

    # 3. Output.
    sections = [_output(s, loaded, units, all_units) for s in merged]
    laptop = loaded[0]
    return SiteReportDocument(
        link=laptop.manifest.final_url or laptop.manifest.input_url,
        title=laptop.manifest.title,
        description=laptop.manifest.description,
        breakpoints={r.bp: r.manifest.viewport for r in loaded},
        sections=sections,
    )


def _load(bp: str, run_path: Path) -> _Run:
    manifest = Manifest.model_validate_json((run_path / "manifest.json").read_text())
    props = ProposalSnapshot.model_validate_json((run_path / "proposals.json").read_text())
    review = review_mod.load(run_path)
    if review is not None and review.proposal_revision != review_mod.proposal_revision(run_path):
        review = None  # predates the current proposals
    rows, members = section_rows(props, review)
    rows = sorted((r for r in rows if r[3] != "excluded"), key=lambda r: r[4].y)
    elements = ElementSnapshot.model_validate_json((run_path / "elements.json").read_text())
    by_id = {e.id: e for e in elements.elements}
    messages = {r[0]: _message(_with_unclaimed_descendants(r[0], members, by_id), by_id) for r in rows}
    return _Run(bp, manifest, by_id, rows, members, messages)


def _leaves(entries) -> list[tuple[str, str, str]]:
    """(tag, text, element id) of every item, list item and list-item part, in order."""
    out = []
    for e in entries:
        if isinstance(e, MessageGroup):
            out += _leaves(e.children)
        elif isinstance(e, MessageListItem):
            out.append(("li", " ".join(c.text for c in e.children), e._eid))
            out += _leaves(e.children)
        else:
            out.append((e.tag, e.text, e._eid))
    return out


def _nodes(entries, bp, units) -> list:
    def node(e):
        if isinstance(e, MessageListItem):
            return _Node(units[(bp, e._eid)], [node(c) for c in e.children])
        return _Node(units[(bp, e._eid)])

    return [_Group(e.tag, [node(c) for c in e.children]) if isinstance(e, MessageGroup) else node(e) for e in entries]


def _placed(merged: list[_Section], current: _Section) -> set[int]:
    """Units already listed in another merged section: never listed twice."""
    out = set()
    for s in merged:
        if s is not current:
            for n in _walk(s.entries):
                out.add(id(n.unit))
    return out


def _walk(entries):
    for e in entries:
        if isinstance(e, _Group):
            yield from _walk(e.children)
        else:
            yield e
            if e.children:
                yield from _walk(e.children)


def _merge_entries(target: list, incoming: list, placed: set[int]) -> None:
    """Add incoming entries whose content is not yet in `target`, each after the last known content
    before it in its own breakpoint's order; groups and list items take new children the same way.
    """
    known = {id(n.unit): n for n in _walk(target)}
    anchor = None  # (list, index) of the last known entry seen in incoming order
    for entry in incoming:
        if isinstance(entry, _Group):
            hit = next((known[id(c.unit)] for c in entry.children if id(c.unit) in known), None)
            group = next((g for g in target if isinstance(g, _Group) and hit in g.children), None)
            if group is not None:
                _merge_children(group.children, entry.children, known, placed)
                anchor = (target, target.index(group))
                continue
            fresh = [c for c in entry.children if id(c.unit) not in known and id(c.unit) not in placed]
            if not fresh:
                if hit is not None and hit in target:
                    anchor = (target, target.index(hit))
                continue
            new = _Group(entry.tag, fresh) if len(fresh) > 1 or entry.tag != "group" else fresh[0]
            _insert(target, anchor, new)
            anchor = (target, target.index(new))
            for n in _walk([new]):
                known[id(n.unit)] = n
            continue
        if id(entry.unit) in known:
            node = known[id(entry.unit)]
            if entry.children and node.children is not None:
                _merge_children(node.children, entry.children, known, placed)
            if node in target:
                anchor = (target, target.index(node))
            continue
        if id(entry.unit) in placed:
            continue
        _insert(target, anchor, entry)
        anchor = (target, target.index(entry))
        for n in _walk([entry]):
            known[id(n.unit)] = n


def _merge_children(target: list[_Node], incoming: list[_Node], known: dict, placed: set[int]) -> None:
    last = -1
    for c in incoming:
        if id(c.unit) in known:
            node = known[id(c.unit)]
            if node in target:
                last = target.index(node)
            continue
        if id(c.unit) in placed:
            continue
        last += 1
        target.insert(last, c)
        known[id(c.unit)] = c


def _insert(target: list, anchor, entry) -> None:
    if anchor is None:
        target.insert(0, entry)
    else:
        target.insert(anchor[1] + 1, entry)


def _output(s: _Section, loaded: list[_Run], units, all_units) -> SiteSection:
    runs = {r.bp: r for r in loaded}
    base = runs[s.base]
    row = next(r for r in base.rows if r[0] == s.present[s.base])
    rid, _label, role, _status, _b, ambiguous, _notes, limitations = row
    flags = (["ambiguous"] if ambiguous else []) + [x.kind for x in limitations]
    flags += [f"overlaps:{k}" for k in _overlap_kinds(rid, role, row[4], base.rows)]
    content = {r[0]: _content_ids(base.members[r[0]], base.by_id) for r in base.rows}
    others = {e for other, ids in content.items() if other != rid for e in ids}

    unit_ids = {id(n.unit) for n in _walk(s.entries)}
    position: dict[str, ReportPosition] = {}
    for bp in ORDER:
        if bp not in runs:
            continue
        run = runs[bp]
        if bp in s.present:
            b = next(r[4] for r in run.rows if r[0] == s.present[bp])
        else:
            # Shown inside another section's region at this breakpoint: the extent of its own content.
            boxes = [run.by_id[u.present[bp]].bounds for u in all_units if id(u) in unit_ids and bp in u.present]
            if not boxes:
                continue
            b = _union(boxes)
        position[bp] = ReportPosition(x=b.x, y=b.y, width=b.width, height=b.height)
    order = [bp for bp in ORDER if bp in runs]
    return SiteSection(
        id=_anchor(content[rid], others, base.by_id),
        position=position,
        hidden=[bp for bp in order if bp not in position],
        flags=flags,
        role=role,
        message=[_emit(e, order) for e in s.entries],
    )


def _hidden(unit: _Unit, order: list[str]) -> list[str]:
    return [bp for bp in order if bp not in unit.present]


def _emit(entry, order):
    if isinstance(entry, _Group):
        children = [_emit(c, order) for c in entry.children]
        shared = [bp for bp in order if all(bp in c.hidden for c in children)]
        return MessageGroup(tag=entry.tag, children=children, hidden=shared)
    if entry.children is not None:
        return MessageListItem(tag="li", children=[_emit(c, order) for c in entry.children], hidden=_hidden(entry.unit, order))
    return MessageItem(tag=entry.unit.tag, text=entry.unit.text, hidden=_hidden(entry.unit, order))


def _union(rects: list[Rect]) -> Rect:
    left, top = min(r.x for r in rects), min(r.y for r in rects)
    right, bottom = max(r.x + r.width for r in rects), max(r.y + r.height for r in rects)
    return Rect(x=left, y=top, width=right - left, height=bottom - top)

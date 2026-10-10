"""Self-contained visual report: screenshot, numbered regions, section list, boundary explanations.

Everything is embedded (the screenshot as a data URI) and every page-supplied string is escaped;
no page script or remote resource is replayed.
"""

import base64
from html import escape
from pathlib import Path

from .models import (
    ElementSnapshot,
    Manifest,
    MessageGroup,
    MessageItem,
    MessageListItem,
    ProposalSnapshot,
    ReportDocument,
    ReportPosition,
    ReportSection,
    ReviewState,
)
from .review import fingerprint

_STYLE = """
body { margin: 0; font: 14px system-ui, sans-serif; color: #222; display: flex; }
.shot { position: relative; flex: none; }
.shot img { display: block; width: 100%; }
.region { position: absolute; outline: 2px solid #c22; background: rgba(204, 34, 34, .07); }
.region.confirmed { outline-color: #1a7f37; background: rgba(26, 127, 55, .07); }
.region.excluded { outline: 2px dashed #888; background: rgba(0, 0, 0, .15); }
.region.ambiguous { outline-style: dashed; }
.region b { position: absolute; top: 0; left: 0; padding: 1px 6px; background: #c22; color: #fff; }
.region.confirmed b { background: #1a7f37; } .region.excluded b { background: #888; }
aside { padding: 16px 24px; min-width: 520px; max-height: 100vh; overflow: auto; position: sticky; top: 0; }
table { border-collapse: collapse; margin-bottom: 24px; width: 100%; }
td, th { border: 1px solid #ddd; padding: 3px 6px; text-align: left; vertical-align: top; }
.split { color: #1a7f37; } .ambiguous { color: #b45309; } .keep { color: #666; }
ul { margin: 2px 0; padding-left: 18px; }
"""


def section_rows(props: ProposalSnapshot, review: ReviewState | None):
    """(id, label, role, status, bounds, ambiguous, notes, limitations) per region, and element members."""
    if review is not None:
        rows = [(r.id, r.label, r.role, r.status, r.bounds, r.ambiguous, r.notes, r.limitations) for r in review.regions]
        return rows, {r.id: r.element_ids for r in review.regions}
    rows = [(s.id, s.label, s.role, "proposed", s.bounds, s.ambiguous, s.notes, s.limitations) for s in props.sections]
    return rows, {s.id: s.element_ids for s in props.sections}


def write_report(run: Path, review: ReviewState | None) -> Path:
    manifest = Manifest.model_validate_json((run / "manifest.json").read_text())
    props = ProposalSnapshot.model_validate_json((run / "proposals.json").read_text())
    png = base64.b64encode((run / manifest.screenshot.path).read_bytes()).decode()
    css_width = manifest.screenshot.width_px / manifest.device_scale_factor  # scale from real pixel size

    rows, members = section_rows(props, review)
    revision = review.revision if review is not None else None

    boxes, items = [], []
    for n, (rid, label, role, status, b, ambiguous, notes, limits) in enumerate(rows, 1):
        cls = " ".join(["region", status, "ambiguous" if ambiguous else ""])
        boxes.append(
            f'<div class="{cls}" style="left:{b.x}px;top:{b.y}px;width:{b.width}px;height:{b.height}px">'
            f"<b>{n}</b></div>"
        )
        flag = (" · ambiguous boundary" if ambiguous else "") + (" · hidden content" if limits else "")
        notes = [describe_limitation(x) for x in limits] + notes
        items.append(
            f"<tr><td>{n}</td><td>{escape(rid)}</td><td>{escape(label)}</td><td>{role}</td>"
            f"<td>{status}{flag}</td><td>{b.y:.0f}–{b.y + b.height:.0f}</td>"
            f"<td>{''.join(f'<div>{escape(x)}</div>' for x in notes)}</td></tr>"
        )

    def ul(xs):
        return "<ul>" + "".join(f"<li>{escape(x)}</li>" for x in xs) + "</ul>" if xs else ""

    def num(v, digits=0):
        return "unknown" if v is None else f"{v:.{digits}f}"

    explain = []
    for b in props.boundaries:
        explain.append(
            f"<tr><td>{escape(b.id)}{' (inner)' if b.level else ''}</td><td>{b.y:.0f}</td>"
            f'<td class="{b.decision}"><b>{b.decision}</b><br>{b.basis}</td>'
            f"<td>g {num(b.gap)}<br>sA {num(b.spacing_above.value)} ({b.spacing_above.observations})"
            f"<br>sB {num(b.spacing_below.value)} ({b.spacing_below.observations})"
            f"<br>floor {b.spacing_floor:.0f} ({b.spacing_floor_source})<br>ratio {num(b.gap_ratio, 2)}</td>"
            f"<td>{ul(b.supporting)}</td><td>{ul(b.opposing)}{ul(['unknown: ' + u for u in b.unknowns])}</td>"
            f"<td>{ul(b.rules)}</td></tr>"
        )

    meta = f'<meta name="review-fingerprint" content="{fingerprint(review)}">' if review is not None else ""
    title = escape(f"Sections — {manifest.final_url or manifest.input_url}")
    html = f"""<!doctype html>
<html><head><meta charset="utf-8">{meta}<title>{title}</title><style>{_STYLE}</style></head>
<body>
<div class="shot" style="width:{css_width}px"><img src="data:image/png;base64,{png}" alt="Captured page">{"".join(boxes)}</div>
<aside>
<h2>{title}</h2>
<p>Capture {escape(manifest.capture_id)} · viewport {manifest.viewport.width}×{manifest.viewport.height}
 · detector {escape(props.detector_version)} (threshold {props.config.gap_ratio_threshold})
 · {"review revision " + str(revision) if revision is not None else "automatic proposals, not reviewed"}</p>
<h3>Sections</h3>
<table><tr><th>#</th><th>ID</th><th>Label</th><th>Role</th><th>Status</th><th>y (CSS px)</th><th>Notes</th></tr>
{"".join(items)}</table>
<h3>Boundary decisions</h3>
<table><tr><th>Boundary</th><th>y</th><th>Decision</th><th>Measurements</th><th>Supporting</th><th>Opposing / unknown</th><th>Rules</th></tr>
{"".join(explain)}</table>
</aside>
</body></html>
"""
    out = run / "report.html"
    out.write_text(html)
    _write_json(run, manifest, rows, members)
    return out


def _write_json(run: Path, manifest: Manifest, rows, members: dict[str, list[str]]) -> None:
    """report.json: link, title, description and each section's position, flags, role and message."""
    elements = ElementSnapshot.model_validate_json((run / "elements.json").read_text())
    by_id = {e.id: e for e in elements.elements}
    retained = [row for row in rows if row[3] != "excluded"]
    content = {row[0]: _content_ids(members[row[0]], by_id) for row in retained}
    sections = []
    for rid, _label, role, status, b, ambiguous, _notes, limitations in sorted(rows, key=lambda row: row[4].y):
        if status == "excluded":
            continue
        flags = (["ambiguous"] if ambiguous else []) + [f"{x.kind}" for x in limitations]
        flags += [f"overlaps:{kind}" for kind in _overlap_kinds(rid, role, b, rows)]
        others = {e for other, ids in content.items() if other != rid for e in ids}
        sections.append(ReportSection(
            id=_anchor(content[rid], others, by_id),
            position=ReportPosition(x=b.x, y=b.y, width=b.width, height=b.height), flags=flags, role=role,
            message=_message(_with_unclaimed_descendants(rid, members, by_id), by_id),
        ))
    doc = ReportDocument(
        link=manifest.final_url or manifest.input_url,
        title=manifest.title,
        description=manifest.description,
        sections=sections,
    )
    (run / "report.json").write_text(doc.model_dump_json(indent=2, exclude_defaults=True))


TEXT_BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}


def _message(element_ids: list[str], by_id) -> list[MessageItem | MessageListItem | MessageGroup]:
    """The section's visible text in page order, grouped by the page's DOM branches (see _by_branch).

    A heading, paragraph or list item is one entry with its whole text (inline links included), so text
    inside it is not repeated; other visible text is "text". Nesting is never more than one level, and
    a group with no siblings is unwrapped: its items are the message.
    """
    entries = _by_branch(set(element_ids), by_id)
    if len(entries) == 1 and isinstance(entries[0], MessageGroup) and entries[0].tag == "group":
        return list(entries[0].children)  # same objects: their source elements stay attached
    return entries


def _text_of(e) -> str:
    return e.full_text if e.full_text is not None else e.text  # before extractor 3: preview only


def _descendants(eid, by_id, skip_lists: bool = False):
    """Descendant IDs; with skip_lists, not those inside nested ul/ol (a list item's text leaves them out)."""
    stack = list(by_id[eid].child_ids)
    while stack:
        c = stack.pop()
        if skip_lists and by_id[c].tag in ("ul", "ol"):
            continue
        yield c
        stack.extend(by_id[c].child_ids)


def _in_message(e) -> bool:
    """DOM content counts even when not painted right now (closed <details>, waiting for an animation,
    a carousel slide or off-canvas menu outside the page area); text meant only for screen readers
    (1x1 boxes, clipped to nothing) does not.
    """
    v = e.visibility
    return not (v.tiny or v.clipped == "hidden" or v.clipped_ancestor)


def _leaf_items(roots, members: set[str], by_id) -> list[MessageItem | MessageListItem]:
    """Text items under these roots in page order. A list item is one li entry; when it holds headings or
    paragraphs of its own, it keeps them as its children instead of one run of text.
    """
    ids = {r for r in roots if r in members} | {d for r in roots for d in _descendants(r, by_id) if d in members}
    items, used = [], set()
    for eid in sorted(ids, key=_order):
        e = by_id[eid]
        if eid in used or not _in_message(e):
            continue
        if e.tag == "a" and not any(by_id[d].tag in TEXT_BLOCKS for d in _descendants(eid, by_id)):
            # A link with inline content only (a button, a nav link): one "a" entry with its whole label.
            # A link wrapping headings or paragraphs (a card) is a container; an image-only link leaves
            # its image to be listed by alt text.
            if text := _text_of(e):
                used.update(_descendants(eid, by_id))
                items.append(_item("a", text, eid))
            continue
        if e.tag in TEXT_BLOCKS:
            inside = set(_descendants(eid, by_id, skip_lists=e.tag == "li"))
            used.update(inside)
            if e.tag == "li" and any(by_id[d].tag in TEXT_BLOCKS - {"li"} for d in inside if d in members):
                parts = _leaf_items([c for c in e.child_ids if by_id[c].tag not in ("ul", "ol")], members, by_id)
                if parts:
                    entry = MessageListItem(tag="li", children=parts)
                    entry._eid = eid
                    items.append(entry)
                continue
        if e.tag == "img":
            # Every image counts: its alt text when it has one, and its source URL.
            alt = " ".join((e.alt or "").split()) or None
            if alt or e.src:
                items.append(_item("img", alt, eid, src=e.src))
            continue
        if text := _text_of(e):
            items.append(_item(e.tag if e.tag in TEXT_BLOCKS else "text", text, eid))
    return items


def _item(tag: str, text: str | None, eid: str, src: str | None = None) -> MessageItem:
    item = MessageItem(tag=tag, text=text, src=src)
    item._eid = eid
    return item


def _by_branch(members: set[str], by_id) -> list[MessageItem | MessageListItem | MessageGroup]:
    """One entry per DOM branch that holds text, one level deep.

    A branch whose parts are single items (a heading and its paragraph, the links of one nav) is a group
    of them; a lone item stays an item (loose text becomes "p"). A branch holding parts that are groups
    themselves (two navs in one wrapper, a row of cards) is split into those parts instead, so a group
    never contains another group. Lists stay whole as ul/ol.
    """
    def items(b):
        return _leaf_items([b], members, by_id)

    def parts(b):
        return [c for c in by_id[b].child_ids if c in members and items(c)]

    def expand(b) -> list[str]:
        e = by_id[b]
        if e.tag in TEXT_BLOCKS or e.tag in ("ul", "ol") or (_in_message(e) and _text_of(e)):
            return [b]  # content itself, not a wrapper
        sub = parts(b)
        if len(sub) == 1:
            return expand(sub[0])  # a wrapper around a single branch
        if any(len(items(c)) > 1 for c in sub):
            return [x for c in sub for x in expand(c)]
        return [b]

    roots = sorted((m for m in members if by_id[m].parent_id not in members), key=_order)
    out: list[MessageItem | MessageListItem | MessageGroup] = []
    for b in (x for r in roots if items(r) for x in expand(r)):
        found = items(b)
        if len(found) == 1:
            item = found[0]
            out.append(_item("p", item.text, item._eid) if item.tag == "text" else item)
        else:
            tag = by_id[b].tag if by_id[b].tag in ("ul", "ol") else "group"
            out.append(MessageGroup(tag=tag, children=found))
    return out


MEDIA = {"img", "svg", "video", "picture", "canvas", "iframe", "object", "embed"}


def _content_ids(element_ids: list[str], by_id) -> set[str]:
    """Visible elements that show something themselves: text or media."""
    return {
        e for e in element_ids
        if by_id[e].visibility.visible and (by_id[e].text or by_id[e].tag in MEDIA)
    }


def _anchor(content: set[str], others: set[str], by_id) -> str | None:
    """id of the outermost element that wraps all of this section's content and none of another's.

    It need not be the section's own outer element: a wrapper inside it, or around it, qualifies too.
    """
    if not content:
        return None

    def chain(eid):  # the element and its ancestors, innermost first
        out = []
        while eid is not None:
            out.append(eid)
            eid = by_id[eid].parent_id
        return out

    common = set(chain(next(iter(content))))
    for eid in content:
        common &= set(chain(eid))
    best = None
    for eid in chain(next(iter(content))):  # innermost to outermost
        if eid not in common or not by_id[eid].html_id:
            continue
        stack, inside = [eid], set()
        while stack:
            x = stack.pop()
            inside.add(x)
            stack.extend(by_id[x].child_ids)
        if inside & others:
            break  # from here outwards every ancestor also wraps another section's content
        best = by_id[eid].html_id
    return best


def _order(eid: str) -> int:
    return int(eid[1:])  # element IDs follow document order


def _with_unclaimed_descendants(rid: str, members: dict[str, list[str]], by_id) -> list[str]:
    """The section's elements plus DOM content nested in them that no section claims.

    Detection groups only visible elements, so text waiting for an animation (visibility: hidden,
    opacity 0) belongs to no section; for the message it belongs to the section that wraps it.
    """
    own = set(members[rid])
    claimed = {e for ids in members.values() for e in ids}
    extra = {d for m in own for d in _descendants(m, by_id) if d not in claimed}
    return sorted(own | extra, key=_order)


def _overlap_kinds(rid, role, b, rows) -> list[str]:
    kinds = []
    for other, _l, other_role, status, ob, *_ in rows:
        if other == rid or status == "excluded":
            continue
        w = min(b.x + b.width, ob.x + ob.width) - max(b.x, ob.x)
        h = min(b.y + b.height, ob.y + ob.height) - max(b.y, ob.y)
        if w > 2 and h > 2:
            roles = {role, other_role}
            kinds.append("overlay" if "overlay" in roles else "header_over_content" if "header" in roles else "content")
    return sorted(set(kinds))


def describe_limitation(x) -> str:
    return (f"hidden content at y {x.top:.0f}–{x.bottom:.0f} ({x.element_count} elements captured invisible, "
            "e.g. revealed only while scrolling): not inspected, not empty")


def report_fingerprint(run: Path) -> str | None:
    """Fingerprint of the review state the current report shows, or None (no report, or not from a review)."""
    path = run / "report.html"
    if not path.exists():
        return None
    marker = '<meta name="review-fingerprint" content="'
    head = path.read_text()[:2000]
    if marker not in head:
        return None
    return head.split(marker, 1)[1].split('"', 1)[0]

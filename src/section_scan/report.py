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
    Message,
    MessageItem,
    MessageList,
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


def write_report(run: Path, review: ReviewState | None) -> Path:
    manifest = Manifest.model_validate_json((run / "manifest.json").read_text())
    props = ProposalSnapshot.model_validate_json((run / "proposals.json").read_text())
    png = base64.b64encode((run / manifest.screenshot.path).read_bytes()).decode()
    css_width = manifest.screenshot.width_px / manifest.device_scale_factor  # scale from real pixel size

    if review is not None:
        rows = [(r.id, r.label, r.role, r.status, r.bounds, r.ambiguous, r.notes, r.limitations) for r in review.regions]
        members = {r.id: r.element_ids for r in review.regions}
        revision = review.revision
    else:
        rows = [(s.id, s.label, s.role, "proposed", s.bounds, s.ambiguous, s.notes, s.limitations) for s in props.sections]
        members = {s.id: s.element_ids for s in props.sections}
        revision = None

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
            message=_message(members[rid], by_id),
        ))
    doc = ReportDocument(
        link=manifest.final_url or manifest.input_url,
        title=manifest.title,
        description=manifest.description,
        sections=sections,
    )
    (run / "report.json").write_text(doc.model_dump_json(indent=2))


TEXT_BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}


def _message(element_ids: list[str], by_id) -> Message:
    """The section's visible text in page order, flat: a heading followed by a paragraph are siblings.

    A heading, paragraph or list item is one entry with its whole text (inline links included), so text
    inside it is not repeated; other visible text is "text". A ul/ol is the only nesting: its items, and
    those of lists nested in them, become its children, never a list inside a list.
    """
    members = set(element_ids)
    items, used = [], set()

    def text_of(e) -> str:
        return e.full_text if e.full_text is not None else e.text  # before extractor 3: preview only

    def descendants(eid):
        stack = list(by_id[eid].child_ids)
        while stack:
            c = stack.pop()
            yield c
            stack.extend(by_id[c].child_ids)

    for eid in sorted(members, key=_order):
        e = by_id[eid]
        if eid in used or not e.visibility.visible:
            continue
        if e.tag in ("ul", "ol"):
            inner = sorted(descendants(eid), key=_order)
            used.update(inner)
            children = [
                MessageItem(tag="li", text=text_of(by_id[c]))
                for c in inner
                if by_id[c].tag == "li" and c in members and by_id[c].visibility.visible and text_of(by_id[c])
            ]
            if children:
                items.append(MessageList(tag=e.tag, children=children))
            continue
        if e.tag in TEXT_BLOCKS:
            used.update(descendants(eid))  # already part of the block's text
        if text := text_of(e):
            items.append(MessageItem(tag=e.tag if e.tag in TEXT_BLOCKS else "text", text=text))
    return Message(content=items)


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

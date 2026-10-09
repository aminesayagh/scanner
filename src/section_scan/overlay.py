from html import escape
from pathlib import Path

from .models import CandidateSnapshot, ElementSnapshot, GroupSnapshot, Manifest

_STYLE = """
body { margin: 0; font: 13px system-ui, sans-serif; }
.page { position: relative; }
.page img { display: block; width: 100%; }
.box { position: absolute; outline: 2px solid #d11; background: rgba(221, 17, 17, .08); }
.box.fixed, .box.sticky { outline-color: #16c; background: rgba(17, 102, 204, .08); }
.box.hidden { outline-style: dashed; background: none; }
.box span { position: absolute; top: 0; left: 0; padding: 0 3px; background: #d11; color: #fff; font-size: 11px; }
.box.fixed span, .box.sticky span { background: #16c; }
table { border-collapse: collapse; margin: 16px; }
td, th { border: 1px solid #ccc; padding: 2px 6px; text-align: left; }
"""


def write_overlay(run_dir: Path, ids: list[str], group_ids: list[str] = (), regions: bool = False) -> Path:
    """Draw selected elements, visual groups, or top-level candidate regions over the screenshot.

    Default: the body's children.
    """
    manifest = Manifest.model_validate_json((run_dir / "manifest.json").read_text())
    if regions:
        cands = CandidateSnapshot.model_validate_json((run_dir / "candidates.json").read_text())
        positioning = {"overlay": "fixed", "header": "sticky"}
        rows = [
            (r.id, r.role, positioning.get(r.role, "flow"), True, r.bounds, "; ".join([" ".join(r.group_ids), *r.notes]))
            for r in cands.regions
            if r.level == 0
        ]
    elif group_ids:
        groups = {g.id: g for g in GroupSnapshot.model_validate_json((run_dir / "groups.json").read_text()).groups}
        _check_known(group_ids, groups, "group")
        snapshot = ElementSnapshot.model_validate_json((run_dir / "elements.json").read_text())
        tags = {e.id: e.tag for e in snapshot.elements}
        # (id, label, positioning, visible, bounds, notes)
        rows = [
            (g.id, f"{g.kind} <{tags[g.primary_element_id]}>", g.positioning, True, g.bounds, "; ".join(g.reasons))
            for g in (groups[i] for i in group_ids)
        ]
    else:
        snapshot = ElementSnapshot.model_validate_json((run_dir / "elements.json").read_text())
        by_id = {e.id: e for e in snapshot.elements}
        _check_known(ids, by_id, "element")
        if not snapshot.elements:
            raise ValueError("The capture has no extracted elements to draw.")
        rows = [
            (e.id, e.tag, e.positioning, e.visibility.visible, e.bounds, e.text)
            for e in (by_id[i] for i in ids or snapshot.elements[0].child_ids)
        ]

    # Size the image in CSS pixels, derived from its real pixel width, so CSS-pixel bounds line up.
    css_width = manifest.screenshot.width_px / manifest.device_scale_factor

    boxes, cells = [], []
    for id_, label, positioning, visible, b, notes in rows:
        classes = " ".join(["box", positioning, "" if visible else "hidden"])
        boxes.append(
            f'<div class="{classes}" style="left:{b.x}px;top:{b.y}px;width:{b.width}px;height:{b.height}px">'
            f"<span>{escape(f'{id_} {label}')}</span></div>"
        )
        cells.append(
            f"<tr><td>{escape(id_)}</td><td>{escape(label)}</td><td>{positioning}</td>"
            f"<td>{'yes' if visible else 'no'}</td>"
            f"<td>{b.x:.0f}, {b.y:.0f}, {b.width:.0f} × {b.height:.0f}</td><td>{escape(notes)}</td></tr>"
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Overlay {escape(manifest.capture_id)}</title><style>{_STYLE}</style></head>
<body>
<div class="page" style="width:{css_width}px"><img src="{escape(manifest.screenshot.path)}" alt="Captured page">
{"".join(boxes)}
</div>
<table><tr><th>ID</th><th>What</th><th>Positioning</th><th>Visible</th><th>Bounds (CSS px)</th><th>Notes</th></tr>
{"".join(cells)}
</table>
</body></html>
"""
    out = run_dir / "overlay.html"
    out.write_text(html)
    return out


def _check_known(ids, known, what: str) -> None:
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"Unknown {what} IDs: {', '.join(unknown)}")


from html import escape
from pathlib import Path

from .models import ElementSnapshot, Manifest

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


def write_overlay(run_dir: Path, ids: list[str]) -> Path:
    """Draw the selected elements' document bounds over the screenshot. Default: the body's children."""
    manifest = Manifest.model_validate_json((run_dir / "manifest.json").read_text())
    snapshot = ElementSnapshot.model_validate_json((run_dir / "elements.json").read_text())
    by_id = {e.id: e for e in snapshot.elements}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"Unknown element IDs: {', '.join(unknown)}")
    if not snapshot.elements:
        raise ValueError("The capture has no extracted elements to draw.")
    selected = [by_id[i] for i in ids or snapshot.elements[0].child_ids]

    # Size the image in CSS pixels, derived from its real pixel width, so CSS-pixel bounds line up.
    css_width = manifest.screenshot.width_px / manifest.device_scale_factor

    boxes, rows = [], []
    for e in selected:
        b = e.bounds
        classes = " ".join(["box", e.positioning, "" if e.visibility.visible else "hidden"])
        label = escape(f"{e.id} {e.tag}")
        boxes.append(
            f'<div class="{classes}" style="left:{b.x}px;top:{b.y}px;width:{b.width}px;height:{b.height}px">'
            f"<span>{label}</span></div>"
        )
        rows.append(
            f"<tr><td>{escape(e.id)}</td><td>{escape(e.tag)}</td><td>{e.positioning}</td>"
            f"<td>{'yes' if e.visibility.visible else 'no'}</td>"
            f"<td>{b.x:.0f}, {b.y:.0f}, {b.width:.0f} × {b.height:.0f}</td><td>{escape(e.text)}</td></tr>"
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Overlay {escape(manifest.capture_id)}</title><style>{_STYLE}</style></head>
<body>
<div class="page" style="width:{css_width}px"><img src="{escape(manifest.screenshot.path)}" alt="Captured page">
{"".join(boxes)}
</div>
<table><tr><th>ID</th><th>Tag</th><th>Positioning</th><th>Visible</th><th>Bounds (CSS px)</th><th>Text</th></tr>
{"".join(rows)}
</table>
</body></html>
"""
    out = run_dir / "overlay.html"
    out.write_text(html)
    return out

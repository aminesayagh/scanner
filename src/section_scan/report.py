"""Self-contained visual report: screenshot, numbered regions, section list, boundary explanations.

Everything is embedded (the screenshot as a data URI) and every page-supplied string is escaped;
no page script or remote resource is replayed.
"""

import base64
from html import escape
from pathlib import Path

from .models import Manifest, ProposalSnapshot, ReviewState
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
        revision = review.revision
    else:
        rows = [(s.id, s.label, s.role, "proposed", s.bounds, s.ambiguous, s.notes, s.limitations) for s in props.sections]
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
    return out


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

"""Export the reviewed section list as validated JSON (milestone 7). Never revisits the site."""

from pathlib import Path

from . import review as review_mod
from .models import ExportDocument, ExportRegion, Manifest, ProposalSnapshot


class ExportError(ValueError):
    pass


def build(run: Path, draft: bool = False) -> ExportDocument:
    manifest = Manifest.model_validate_json((run / "manifest.json").read_text())
    props = ProposalSnapshot.model_validate_json((run / "proposals.json").read_text())
    state = review_mod.load(run)
    current = review_mod.proposal_revision(run)
    if state is not None and state.proposal_revision != current:
        raise ExportError("the review predates the current proposals; run `review --restart` and review them")

    if state is None:
        if not draft:
            raise ExportError("nothing has been reviewed yet; run `review`, or export a --draft of the proposals")
        rows = [
            (s.id, s.label, s.role, "proposed", s.bounds, s.element_ids, [s.id], s.limitations) for s in props.sections
        ]
    else:
        retained = [r for r in state.regions if r.status != "excluded"]
        outstanding = [r for r in retained if r.status != "confirmed"]
        if outstanding and not draft:
            listed = ", ".join(f"{r.id} ({r.label})" for r in outstanding)
            raise ExportError(f"{len(outstanding)} region(s) still need review: {listed}")
        rows = [
            (r.id, r.label, r.role, r.status, r.bounds, r.element_ids, r.source_section_ids, r.limitations)
            for r in retained
        ]
    if not rows and not draft:
        raise ExportError("every region is excluded; there is nothing to export")
    rows.sort(key=lambda row: row[4].y)  # `order` is top-to-bottom page order, overlays included

    regions = [
        ExportRegion(
            id=rid, order=i, label=label, role=role, status=status, bounds=bounds,
            element_ids=elements, source_section_ids=sources, overlaps=[], limitations=limits,
        )
        for i, (rid, label, role, status, bounds, elements, sources, limits) in enumerate(rows)
    ]
    for i, a in enumerate(regions):  # explicit overlaps among what is exported
        for b in regions[i + 1 :]:
            w = min(a.bounds.x + a.bounds.width, b.bounds.x + b.bounds.width) - max(a.bounds.x, b.bounds.x)
            h = min(a.bounds.y + a.bounds.height, b.bounds.y + b.bounds.height) - max(a.bounds.y, b.bounds.y)
            if w > 2 and h > 2:
                roles = {a.role, b.role}
                kind = "overlay" if "overlay" in roles else "header_over_content" if "header" in roles else "content"
                a.overlaps.append((b.id, kind))
                b.overlaps.append((a.id, kind))

    return ExportDocument(
        kind="draft-sections" if draft else "sections",
        capture_id=manifest.capture_id,
        page_url=manifest.input_url,
        final_url=manifest.final_url,
        viewport=manifest.viewport,
        device_scale_factor=manifest.device_scale_factor,
        detector_version=props.detector_version,
        proposal_revision=current,
        review_revision=state.revision if state else None,
        regions=regions,
    )

"""Find page structure, candidate regions and the boundaries between them (milestone 4).

Records evidence only; deciding which boundaries are real section breaks is the detector's job.
"""

from collections.abc import Iterator

from .models import (
    BoundaryCandidate,
    BoundaryEvidence,
    CandidateSnapshot,
    GroupSnapshot,
    Overlap,
    PageStructure,
    Rect,
    RegionCandidate,
    VisualGroup,
)

# Bump when candidate rules change, so old candidates.json files stay identifiable.
CANDIDATES_VERSION = "1"

# ponytail: structural constant; promote to detector.toml if evaluation shows it needs tuning.
TOLERANCE = 2.0  # CSS px treated as touching / not crossing
TOP_EDGE = 10.0  # a fallback header must start this close to the top
HEADER_MAX_TOP = 200.0  # a header landmark must start within this distance of the top...
HEADER_MAX_HEIGHT = 300.0  # ...and be at most this tall; otherwise it is content (often a hero)
FOOTER_MAX_GAP = 100.0  # a footer landmark must end this close to the page bottom...
FOOTER_MIN_WIDTH = 0.5  # ...and span at least this share of the page width
DOMINANT_PAINT_HEIGHT = 0.5  # a painted descendant covering this share of a region's height colours it
FULL_WIDTH = 0.9  # ...and span at least this share of the page width

HEADER_LANDMARKS = {"header", "banner"}
FOOTER_LANDMARKS = {"footer", "contentinfo"}
# Landmarks that make a nested header/footer belong to their content rather than to the page.
SECTIONING = {"main", "article", "section", "aside", "header", "banner", "footer", "contentinfo"}
# Child landmarks that suggest a container holds several distinct regions.
REGION_LANDMARKS = {"section", "article", "aside", "header", "footer", "region"}


def generate(groups: GroupSnapshot) -> CandidateSnapshot:
    by_id = {g.id: g for g in groups.groups}
    notes: list[str] = []
    roots = [g for g in groups.groups if g.parent_id is None]
    if not roots:
        return _empty(groups, "no visual groups: nothing to analyse")
    if len(roots) > 1:
        notes.append(f"{len(roots)} root groups (page root had no visible box); treated as one page")
    page_width = max(r.bounds.width for r in roots)

    def ancestors(g: VisualGroup) -> Iterator[VisualGroup]:
        while g.parent_id:
            g = by_id[g.parent_id]
            yield g

    def page_level(g: VisualGroup) -> bool:
        return not any(a.evidence.landmark in SECTIONING for a in ancestors(g))

    # A page header sits at the top and is bar-sized; a tall <header> lower down is usually a hero banner.
    header = None
    for g in groups.groups:
        if g.evidence.landmark in HEADER_LANDMARKS and page_level(g):
            if g.bounds.y <= HEADER_MAX_TOP and g.bounds.height <= HEADER_MAX_HEIGHT:
                header = g
                notes.append(f"header: landmark {g.id}")
                break
            notes.append(
                f"header: landmark {g.id} rejected (top {g.bounds.y:.0f}, height {g.bounds.height:.0f}); "
                "treated as content, likely a hero"
            )
    # A page footer ends the page: in flow, near the bottom, and wide. A quote attribution or a footer
    # inside a cookie dialog is not the page footer.
    page_bottom = max(r.bounds.y + r.bounds.height for r in roots)
    footer = None
    for g in reversed(groups.groups):
        if g.evidence.landmark in FOOTER_LANDMARKS and page_level(g):
            b = g.bounds
            if (
                g.positioning in ("flow", "sticky")
                and b.y + b.height >= page_bottom - FOOTER_MAX_GAP
                and b.width >= FOOTER_MIN_WIDTH * page_width
            ):
                footer = g
                notes.append(f"footer: landmark {g.id}")
                break
            notes.append(f"footer: landmark {g.id} rejected ({g.positioning}, bottom {b.y + b.height:.0f}, width {b.width:.0f})")
    if footer is None:
        notes.append("footer: none found")
    main = next((g for g in groups.groups if g.evidence.landmark == "main"), None)
    notes.append(f"main: landmark {main.id}" if main else "main: no landmark; using the page's body children")
    contains_main = {main.id, *(a.id for a in ancestors(main))} if main else set()

    special = {g.id for g in (header, footer) if g}

    def flow_children(g: VisualGroup) -> list[VisualGroup]:
        kids = [by_id[c] for c in g.child_ids]
        return [k for k in kids if k.positioning in ("flow", "sticky") and k.bounds.height > 0 and k.id not in special]

    # Top-level candidates: page flow, with <main> and the wrappers around it opened up,
    # and a single wrapper spanning everything looked through.
    top = [by_id[c] for r in roots for c in r.child_ids] if len(roots) == 1 else roots
    items = [g for g in top if g.positioning in ("flow", "sticky") and g.bounds.height > 0 and g.id not in special]
    def opens_cleanly(g: VisualGroup) -> bool:
        # Opening is safe unless main (or a wrapper of it) shares a row with siblings, e.g. a sidebar:
        # flattening would mix the main column's sections with the sidebar.
        return all(len(b) == 1 for b in _bands(flow_children(g)) if any(x.id in contains_main for x in b))

    seen = list(top)  # every group directly inside the wrappers walked through
    blocked: set[str] = set()
    while True:
        holders = [g for g in items if g.id in contains_main and g.id not in blocked]
        for g in holders:
            if not opens_cleanly(g):
                blocked.add(g.id)
                notes.append(f"kept {g.id} whole: main shares a row with side content; main's regions are alternatives")
        if opened := [g for g in holders if g.id not in blocked]:
            items = [k for g in items for k in (flow_children(g) if g in opened else [g])]
            notes.extend(f"opened {g.id}: holds the main landmark" for g in opened if g.id != main.id)
        elif len(items) == 1 and flow_children(items[0]) and items[0].id not in blocked:
            opened = items
            notes.append(f"looked inside {items[0].id}: one wrapper spans the whole flow")
            items = flow_children(items[0])
        else:
            break
        seen += [by_id[c] for g in opened for c in g.child_ids]

    if header is None:
        # Fallback: a full-width bar pinned at the top, among the groups the walk above passed through.
        header = next(
            (
                g
                for g in seen
                if g.positioning != "flow"
                and g.bounds.y <= TOP_EDGE
                and g.bounds.width >= FULL_WIDTH * page_width
                and g.bounds.height <= HEADER_MAX_HEIGHT
                and _content_boxes([g], by_id)
            ),
            None,
        )
        if header:
            notes.append(f"header: fallback {header.id}, a full-width {header.positioning} bar at the top")
            items = [g for g in items if g.id != header.id]
        else:
            notes.append("header: none found")

    special |= {header.id} if header else set()  # a fallback header leaves the flow too
    loose = _content_boxes([g for g in seen if g.positioning == "absolute" and g.id not in special], by_id)

    regions: list[RegionCandidate] = []
    boundaries: list[BoundaryCandidate] = []

    def add_region(role: str, members: list[VisualGroup], level: int = 0, parent: str | None = None, note=()):
        r = RegionCandidate(
            id=f"r{len(regions)}",
            role=role,
            level=level,
            parent_region_id=parent,
            group_ids=[g.id for g in members],
            bounds=_union([g.bounds for g in members]),
            notes=list(note),
        )
        regions.append(r)
        return r

    if header:
        add_region("header", [header])
    flow = [add_region("flow", band, note=_band_note(band)) for band in _bands(items)]
    if footer:
        add_region("footer", [footer])
    covered = {g.id for g in (header, footer) if g}
    for g in groups.groups:
        # Outermost fixed groups outside the header/footer: cookie banners, chat buttons, skip links.
        outermost = g.parent_id is None or by_id[g.parent_id].positioning != "fixed"
        inside_special = any(a.id in covered for a in ancestors(g))
        if g.positioning == "fixed" and outermost and g.id not in covered and not inside_special:
            add_region("overlay", [g])
    top_level = list(regions)

    def link(pairs: list[RegionCandidate], level: int) -> None:
        for above, below in zip(pairs, pairs[1:]):
            boundaries.append(_boundary(f"b{len(boundaries)}", level, above, below, by_id, loose))

    # 7. Header and footer get boundary records too, so the record does not depend on landmark use.
    ends = [r for r in top_level if r.role in ("header", "footer")]
    link([r for r in ends if r.role == "header"] + flow + [r for r in ends if r.role == "footer"], 0)

    # Level-1 alternatives: a region whose inside holds visibly distinct sub-regions stays one candidate,
    # but its inner boundaries are kept for inspection instead of being decided here.
    # ponytail: one nesting level; recurse further if real pages show regions nested deeper.
    for r in flow:
        holds_main = main is not None and any(i in contains_main for i in r.group_ids)
        if holds_main:
            kids = flow_children(main)  # the main column's regions, apart from any sidebar
        elif len(r.group_ids) == 1:
            kids = flow_children(by_id[r.group_ids[0]])
        else:
            continue
        while len(kids) == 1:
            kids = flow_children(kids[0])
        sub_bands = _bands(kids)
        if len(sub_bands) >= 2 and (holds_main or _distinct(sub_bands, by_id)):
            subs = [add_region("flow", b, 1, r.id, _band_note(b)) for b in sub_bands]
            link(subs, 1)

    return CandidateSnapshot(
        capture_id=groups.capture_id,
        candidates_version=CANDIDATES_VERSION,
        structure=PageStructure(
            header_group_id=header.id if header else None,
            footer_group_id=footer.id if footer else None,
            main_group_id=main.id if main else None,
            notes=notes,
        ),
        regions=regions,
        boundaries=boundaries,
        overlaps=_overlaps(top_level),
    )


def _empty(groups: GroupSnapshot, note: str) -> CandidateSnapshot:
    return CandidateSnapshot(
        capture_id=groups.capture_id,
        candidates_version=CANDIDATES_VERSION,
        structure=PageStructure(header_group_id=None, footer_group_id=None, main_group_id=None, notes=[note]),
        regions=[],
        boundaries=[],
        overlaps=[],
    )


def _bands(items: list[VisualGroup]) -> list[list[VisualGroup]]:
    """Siblings whose vertical extents overlap share a row and form one band (e.g. a two-column layout).

    Only siblings are compared, so columns elsewhere on the page cannot interleave with them.
    """
    bands: list[list[VisualGroup]] = []
    bottom = None
    for g in sorted(items, key=lambda g: g.bounds.y):
        # Overlapping vertically but not side by side (e.g. a card pulled up by a negative margin) is a
        # stacked overlap: it stays its own band, and the overlap is recorded.
        if bands and g.bounds.y < bottom - TOLERANCE and all(_side_by_side(g, m) for m in bands[-1]):
            bands[-1].append(g)
            bottom = max(bottom, g.bounds.y + g.bounds.height)
        else:
            bands.append([g])
            bottom = g.bounds.y + g.bounds.height
    return bands


def _side_by_side(a: VisualGroup, b: VisualGroup) -> bool:
    a_left, a_right = a.bounds.x, a.bounds.x + a.bounds.width
    b_left, b_right = b.bounds.x, b.bounds.x + b.bounds.width
    return min(a_right, b_right) - max(a_left, b_left) <= TOLERANCE


def _band_note(band: list[VisualGroup]) -> list[str]:
    return [f"row of {len(band)} side-by-side groups"] if len(band) > 1 else []


def _union(rects: list[Rect]) -> Rect:
    left, top = min(r.x for r in rects), min(r.y for r in rects)
    right, bottom = max(r.x + r.width for r in rects), max(r.y + r.height for r in rects)
    return Rect(x=left, y=top, width=right - left, height=bottom - top)


def _dominant_paint(g: VisualGroup, by_id: dict[str, VisualGroup]) -> str | None:
    """Paint of a descendant spanning (nearly) the region's width and most of its height, if any."""
    stack = [by_id[c] for c in g.child_ids]
    while stack:
        d = stack.pop()
        if d.positioning == "fixed":
            continue
        own = d.evidence.background.own
        if (
            own != "transparent"
            and d.bounds.width >= FULL_WIDTH * g.bounds.width
            and d.bounds.height >= DOMINANT_PAINT_HEIGHT * g.bounds.height
        ):
            return own
        stack.extend(by_id[c] for c in d.child_ids)
    return None


def _effective_background(g: VisualGroup, by_id: dict[str, VisualGroup]) -> str:
    bg = g.evidence.background
    if bg.own != "transparent":
        return bg.own
    return _dominant_paint(g, by_id) or bg.context or "canvas"


def _band_background(band: list[VisualGroup], by_id: dict[str, VisualGroup]) -> str:
    values = {_effective_background(g, by_id) for g in band}
    return values.pop() if len(values) == 1 else "complex"


def _distinct(bands: list[list[VisualGroup]], by_id: dict[str, VisualGroup]) -> bool:
    """Whether sub-bands look like separate regions: their own landmarks, or different paint."""
    if any(g.evidence.landmark in REGION_LANDMARKS for band in bands for g in band):
        return True
    return len({_band_background(b, by_id) for b in bands}) > 1


def _content_boxes(members: list[VisualGroup], by_id: dict[str, VisualGroup]) -> list[Rect]:
    """Foreground content (text, media, controls); backgrounds and padded boxes do not count."""
    boxes, stack = [], list(members)
    while stack:
        g = stack.pop()
        if g.positioning == "fixed":
            continue
        if g.kind == "content":
            boxes.append(g.bounds)
        stack.extend(by_id[c] for c in g.child_ids)
    return boxes


def _boundary(bid, level, above: RegionCandidate, below: RegionCandidate, by_id, loose: list[Rect]) -> BoundaryCandidate:
    a_groups = [by_id[i] for i in above.group_ids]
    b_groups = [by_id[i] for i in below.group_ids]
    a_bottom = above.bounds.y + above.bounds.height
    b_top = below.bounds.y
    y = (a_bottom + b_top) / 2

    a_content, b_content = _content_boxes(a_groups, by_id), _content_boxes(b_groups, by_id)
    content_gap = None
    if a_content and b_content:
        content_gap = min(r.y for r in b_content) - max(r.y + r.height for r in a_content)
    spans = (
        any(r.y + r.height > y + TOLERANCE for r in a_content)
        or any(r.y < y - TOLERANCE for r in b_content)
        or any(r.y < y - TOLERANCE and r.y + r.height > y + TOLERANCE for r in loose)  # out-of-flow content
    )

    bg_a, bg_b = _band_background(a_groups, by_id), _band_background(b_groups, by_id)
    change = None if "complex" in (bg_a, bg_b) else bg_a != bg_b
    # Every group on both sides is transparent over the same painted ancestor: its paint runs across.
    backgrounds = [g.evidence.background for g in a_groups + b_groups]
    contexts = {bg.context_element_id for bg in backgrounds}
    shared = None
    dominant = any(_dominant_paint(g, by_id) for g in a_groups + b_groups)
    if all(bg.own == "transparent" for bg in backgrounds) and len(contexts) == 1 and not dominant:
        shared = contexts.pop()

    notes = []
    if content_gap is None:
        notes.append("a side has no foreground content; content gap unknown")
    elif content_gap < 0:
        notes.append("foreground content of the two sides overlaps vertically")
    return BoundaryCandidate(
        id=bid,
        level=level,
        above_region_id=above.id,
        below_region_id=below.id,
        y=y,
        box_gap=b_top - a_bottom,
        content_gap=content_gap,
        evidence=BoundaryEvidence(
            landmarks=(_single_landmark(a_groups), _single_landmark(b_groups)),
            backgrounds=(bg_a, bg_b),
            background_change=change,
            shared_painted_ancestor=shared,
            foreground_spans=spans,
        ),
        notes=notes,
    )


def _single_landmark(band: list[VisualGroup]) -> str | None:
    return band[0].evidence.landmark if len(band) == 1 else None


def _overlaps(regions: list[RegionCandidate]) -> list[Overlap]:
    out = []
    for i, a in enumerate(regions):
        for b in regions[i + 1 :]:
            w = min(a.bounds.x + a.bounds.width, b.bounds.x + b.bounds.width) - max(a.bounds.x, b.bounds.x)
            h = min(a.bounds.y + a.bounds.height, b.bounds.y + b.bounds.height) - max(a.bounds.y, b.bounds.y)
            if w <= TOLERANCE or h <= TOLERANCE:
                continue
            roles = {a.role, b.role}
            kind = "overlay" if "overlay" in roles else "header_over_content" if "header" in roles else "content"
            out.append(Overlap(region_ids=(a.id, b.id), kind=kind, area=w * h))
    return out

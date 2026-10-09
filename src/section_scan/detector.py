"""Decide candidate boundaries and assemble proposed sections (milestone 5).

Rules, checked in order for each boundary (plan, "Decision baseline"):
  1. A header/footer landmark always gets its own section.
  2. Split on a strong relative gap (gap_ratio >= threshold) with independent support: a background change,
     a landmark transition, or a meaningful sibling transition.
  3. Split without a large gap on a clear visual (background change) or structural (landmark) transition.
  4. Keep tight coherent groups and repeated-card collections together unless other evidence supports a split.
  5. Leave conflicts ambiguous: content spanning the line, a strong gap without support, or no evidence at all.

"Meaningful sibling transition": both sides are single groups in the same parent, and each has its own
box evidence (landmark, padding, border or background). Boundaries between generic wrappers are weak
evidence and need a strong gap plus another signal. No weighted scores: raw measurements and rule
outcomes are recorded instead.
"""

import statistics

from .candidates import FULL_WIDTH, REGION_LANDMARKS, _bands, _union
from .models import (
    BoundaryCandidate,
    BoundaryProposal,
    CandidateSnapshot,
    DetectorConfig,
    ElementSnapshot,
    GroupSnapshot,
    ProposalSnapshot,
    SectionLimitation,
    SectionProposal,
    SpacingEstimate,
    VisualGroup,
)

# Bump when decision rules change, so old proposals.json files stay identifiable.
DETECTOR_VERSION = "2"

# Stacking levels that internal spacing is measured in: the region's first stack of rows and the stacks
# directly inside it. A container whose children form one row (a wrapper, or columns side by side) adds
# no level, so wrapped sections are measured as deep as their first rows.
SPACING_DEPTH = 2

# CSS px: a blank-looking band of hidden elements at least this tall is recorded as a limitation.
# ponytail: structural constant; moves to detector.toml with the other constants.
HIDDEN_MIN_HEIGHT = 300.0


def detect(
    elements: ElementSnapshot, groups: GroupSnapshot, cands: CandidateSnapshot, config: DetectorConfig
) -> ProposalSnapshot:
    by_id = {g.id: g for g in groups.groups}
    styles = {e.id: e.styles for e in elements.elements}
    regions = {r.id: r for r in cands.regions}

    page_width = max((g.bounds.width for g in groups.groups if g.parent_id is None), default=0.0)
    boundaries = [_decide(b, regions, by_id, styles, config, page_width) for b in cands.boundaries]
    decided = {b.id: b for b in boundaries}
    sections, notes, unassigned = _assemble(cands, decided, by_id)
    _flag_hidden_content(sections, elements, groups)

    status = "ok" if any(s.role == "flow" for s in sections) else "unsupported"
    if status == "unsupported":
        notes.append("no main-content sections found: analysis unsupported, not a page without content")
    return ProposalSnapshot(
        capture_id=cands.capture_id,
        detector_version=DETECTOR_VERSION,
        config=config,
        status=status,
        notes=notes,
        boundaries=boundaries,
        sections=sections,
        unassigned_group_ids=unassigned,
    )


# --- measurements ---------------------------------------------------------------------------------


def _internal_gaps(members: list[VisualGroup], by_id: dict[str, VisualGroup]) -> list[float]:
    """Positive vertical gaps between consecutive rows of children, in the region's first stacks of rows
    and the stacks directly inside them: compact local groups, not every deeply nested detail.

    Only vertical gaps between stacked rows count: side-by-side children (horizontal gaps) are banded
    into one row first, so button gaps never mix with paragraph spacing. Single-row containers are
    passed through without using up SPACING_DEPTH: theme wrappers would otherwise hide every row.
    """
    gaps, stack = [], [(g, 0) for g in members]
    while stack:
        g, depth = stack.pop()
        kids = [by_id[c] for c in g.child_ids if by_id[c].positioning in ("flow", "sticky")]
        rows = _bands(kids)
        for above, below in zip(rows, rows[1:]):
            bottom = max(k.bounds.y + k.bounds.height for k in above)
            top = min(k.bounds.y for k in below)
            if top - bottom > 0:
                gaps.append(top - bottom)
        level = depth + (len(rows) > 1)  # only a stack of rows is a new level
        if level < SPACING_DEPTH:
            stack.extend((k, level) for k in kids)
    return gaps


def _spacing(members, by_id, config: DetectorConfig) -> SpacingEstimate:
    gaps = _internal_gaps(members, by_id)
    value = statistics.median(gaps) if len(gaps) >= config.min_spacing_observations else None
    return SpacingEstimate(value=value, observations=len(gaps))


def _line_height(style: dict[str, str]) -> float | None:
    lh, fs = style.get("lineHeight", ""), style.get("fontSize", "")
    try:
        if lh.endswith("px"):
            return float(lh[:-2])
        if lh == "normal" and fs.endswith("px"):
            return 1.2 * float(fs[:-2])  # browsers' typical "normal" line height
    except ValueError:
        pass
    return None


def _floor(members, by_id, styles, config: DetectorConfig) -> tuple[float, str]:
    """Robust nearby line height, from content on both sides; the configured fallback otherwise."""
    heights, stack = [], list(members)
    while stack:
        g = stack.pop()
        if g.kind == "content" and (h := _line_height(styles.get(g.primary_element_id, {}))):
            heights.append(h)
        stack.extend(by_id[c] for c in g.child_ids)
    if heights:
        return statistics.median(heights), "line_height"
    return config.fallback_spacing_floor, "fallback"


# --- decisions ------------------------------------------------------------------------------------


def _boxed(g: VisualGroup) -> bool:
    e = g.evidence
    return bool(e.landmark) or e.padding or e.border or e.background.own != "transparent"


def _decide(b: BoundaryCandidate, regions, by_id, styles, config: DetectorConfig, page_width) -> BoundaryProposal:
    above, below = regions[b.above_region_id], regions[b.below_region_id]
    a_groups = [by_id[i] for i in above.group_ids]
    b_groups = [by_id[i] for i in below.group_ids]
    ev = b.evidence

    gap = None if b.content_gap is None else max(0.0, b.content_gap)
    s_above, s_below = _spacing(a_groups, by_id, config), _spacing(b_groups, by_id, config)
    floor, floor_source = _floor(a_groups + b_groups, by_id, styles, config)
    known = [s.value for s in (s_above, s_below) if s.value is not None]
    scale = max(*known, floor) if known else None
    ratio = gap / scale if gap is not None and scale else None  # scale >= floor > 0: no division by zero
    strong_gap = ratio is not None and ratio >= config.gap_ratio_threshold

    supporting, opposing, rules = [], [], []
    # A section-level paint change spans the page; a narrower painted card is a child of its section.
    wide = min(above.bounds.width, below.bounds.width) >= FULL_WIDTH * page_width
    paint_change = bool(ev.background_change) and wide
    if paint_change:
        supporting.append(f"background changes ({ev.backgrounds[0]} → {ev.backgrounds[1]})")
    elif ev.background_change:
        opposing.append("background changes only across a narrower-than-page box, e.g. a card (weak)")
    landmark_transition = ev.landmarks[0] in REGION_LANDMARKS and ev.landmarks[1] in REGION_LANDMARKS
    if landmark_transition:
        supporting.append(f"landmark transition (<{ev.landmarks[0]}> → <{ev.landmarks[1]}>)")
    siblings = (
        len(a_groups) == len(b_groups) == 1
        and a_groups[0].parent_id == b_groups[0].parent_id
        and _boxed(a_groups[0])
        and _boxed(b_groups[0])
    )
    if siblings and not landmark_transition:
        supporting.append("meaningful sibling transition (both sides have their own box evidence)")
    if strong_gap:
        supporting.append(f"strong relative gap (ratio {ratio:.2f} ≥ {config.gap_ratio_threshold})")
    if ev.foreground_spans:
        opposing.append("foreground content spans the boundary")
    parents = {g.parent_id for g in a_groups + b_groups}
    parent = parents.pop() if len(parents) == 1 else None
    repeated = parent is not None and by_id[parent].evidence.repeated_children
    if repeated:
        opposing.append("both sides belong to one repeated collection")
    if ev.shared_painted_ancestor:
        opposing.append(f"same painted ancestor {ev.shared_painted_ancestor} continues across (weak)")
    unknowns = []
    if ev.background_change is None:
        unknowns.append("background comparison (complex painting)")
    if gap is None:
        unknowns.append("content gap (a side has no foreground content)")

    independent = paint_change or landmark_transition or siblings

    def result(decision, basis, rule):
        rules.append(rule)
        return BoundaryProposal(
            id=b.id,
            level=b.level,
            above_region_id=b.above_region_id,
            below_region_id=b.below_region_id,
            y=b.y,
            gap=gap,
            spacing_above=s_above,
            spacing_below=s_below,
            spacing_floor=floor,
            spacing_floor_source=floor_source,
            spacing_scale=scale,
            gap_ratio=ratio,
            supporting=supporting,
            opposing=opposing,
            unknowns=unknowns,
            rules=rules,
            decision=decision,
            basis=basis,
        )

    if {above.role, below.role} & {"header", "footer"}:
        return result("split", "landmark", "1: header/footer landmark keeps its own section")
    rules.append("1: no header/footer landmark involved")

    if config.mode == "gaps":  # baseline: the gap ratio alone
        if ratio is None:
            return result("ambiguous", "none", "gaps baseline: no gap ratio")
        if strong_gap:
            return result("split", "gap", "gaps baseline: ratio at or above threshold")
        return result("keep", "none", "gaps baseline: ratio below threshold")
    if config.mode == "container":  # baseline: structure and paint alone, no gap evidence
        if ev.foreground_spans and independent:
            return result("ambiguous", "none", "container baseline: transition conflicts with spanning content")
        if repeated and not paint_change:
            return result("keep", "none", "container baseline: repeated collection")
        if paint_change:
            return result("split", "visual", "container baseline: page-wide background change")
        if landmark_transition or siblings:
            return result("split", "structural", "container baseline: landmark or boxed sibling transition")
        return result("keep", "none", "container baseline: generic wrappers")

    if ev.foreground_spans and (strong_gap or independent):
        return result("ambiguous", "none", "5: split evidence conflicts with content spanning the line")
    if repeated and not paint_change:
        return result("keep", "none", "4: repeated collection without a background change stays together")
    if strong_gap and independent:
        return result("split", "gap", "2: strong relative gap with independent support")
    rules.append("2: no strong gap with independent support")

    if paint_change:
        return result("split", "visual", "3: page-wide background change without a large gap")
    if landmark_transition:
        return result("split", "structural", "3: landmark transition without a large gap")
    rules.append("3: no clear visual or structural transition")

    if strong_gap:
        return result("ambiguous", "none", "5: strong gap without independent support")
    if gap is None:
        return result("ambiguous", "none", "5: content gap unknown and no structural support")
    if ratio is None:
        return result("ambiguous", "none", "5: no reliable spacing evidence and no structural support")
    return result("keep", "none", "4: tight coherent spacing, no transition: same section")


# --- section assembly -----------------------------------------------------------------------------


def _assemble(cands: CandidateSnapshot, decided: dict[str, BoundaryProposal], by_id):
    """Cut the page at split/ambiguous boundaries; join units across keep boundaries."""
    notes: list[str] = []
    regions = {r.id: r for r in cands.regions}
    between = {(b.above_region_id, b.below_region_id): b for b in decided.values()}
    level0 = [r for r in cands.regions if r.level == 0 and r.role != "overlay"]
    # Header, footer and overlay groups can sit inside a flow region (a header inside the hero wrapper,
    # a fixed chat button inside a section). They are their own sections; flow sections carve them out.
    reserved = {g for r in cands.regions if r.role != "flow" for g in r.group_ids}

    # Each unit is (region IDs, group IDs, role). A region with a decided inner split is replaced by its
    # sub-regions, each examined the same way; groups the sub-regions do not cover (e.g. a sidebar) become
    # their own unit.
    units: list[tuple[list[str], list[str], str, list[BoundaryProposal]]] = []
    joins: list[BoundaryProposal | None] = []  # joins[i] sits between units[i] and units[i + 1]
    extras: list[tuple[list[str], list[str], str]] = []

    def pieces(r) -> list[tuple]:
        """[(region, boundary to the previous piece, undecided inner boundaries)] standing in for r."""
        subs = [s for s in cands.regions if s.parent_region_id == r.id]
        inner = [between[(a.id, c.id)] for a, c in zip(subs, subs[1:]) if (a.id, c.id) in between]
        if not any(b.decision == "split" for b in inner):
            return [(r, None, inner)]
        covered = {g for s in subs for g in _subtree_ids(s.group_ids, by_id)} | _subtree_ids(reserved, by_id)
        for g in (x for g in r.group_ids for x in _leftovers(g, covered, by_id)):
            # In-flow side content (a sidebar) becomes its own section; out-of-flow leftovers
            # are allocated by overlap below like any other loose content.
            if by_id[g].positioning in ("flow", "sticky"):
                extras.append(([r.id], [g], "flow"))
                notes.append(f"{r.id}: inner sections promoted; side content {g} kept as its own section")
        out = []
        for i, s in enumerate(subs):
            # A sub-region's first piece meets the previous sub-region across their shared boundary.
            (first, _, undecided), *rest = pieces(s)
            out += [(first, between.get((subs[i - 1].id, s.id)) if i else None, undecided), *rest]
        return out

    prev_last = None
    for r in level0:
        for i, (p, join, undecided) in enumerate(pieces(r)):
            if units:
                # The first piece meets the previous level-0 region across their level-0 boundary.
                joins.append(between.get((prev_last, r.id)) if i == 0 else join)
            units.append(([p.id], list(p.group_ids), p.role, undecided))
        prev_last = r.id

    merged: list[dict] = []
    for i, (rids, gids, role, undecided) in enumerate(units):
        join = joins[i - 1] if i else None
        if merged and join is not None and join.decision == "keep" and role == merged[-1]["role"] == "flow":
            merged[-1]["region_ids"] += rids
            merged[-1]["group_ids"] += gids
            merged[-1]["kept"].append(join.id)
            merged[-1]["inner"] += undecided
            continue
        if merged:
            merged[-1]["below"] = join
        merged.append(_unit(rids, gids, role, above=join, inner=undecided))
    merged += [_unit(rids, gids, role) for rids, gids, role in extras]
    merged += [_unit([r.id], r.group_ids, "overlay") for r in cands.regions if r.role == "overlay"]
    rank = {"header": 0, "flow": 1, "footer": 2, "overlay": 3}
    merged.sort(key=lambda m: (rank[m["role"]], min(by_id[g].bounds.y for g in m["group_ids"])))  # stable

    def claim(m) -> set[str]:
        # Flow sections stop at reserved groups; header/footer/overlay sections own their whole subtree.
        return _subtree_ids(m["group_ids"], by_id, stop=reserved if m["role"] == "flow" else set())

    sections: list[SectionProposal] = []
    flow_count = 0
    for i, m in enumerate(merged):
        if m["role"] == "flow":
            flow_count += 1
            label = f"Section {flow_count}"
        else:
            label = m["role"].capitalize()
        gs = [by_id[g] for g in m["group_ids"]]
        adjoining = [b for b in (m["above"], m["below"]) if b is not None]
        owned = claim(m)
        carved = sorted(
            g for g in reserved if g not in m["group_ids"] and g in _subtree_ids(m["group_ids"], by_id)
        ) if m["role"] == "flow" else []
        sections.append(
            SectionProposal(
                id=f"s{i}",
                order=i,
                label=label,
                role=m["role"],
                region_ids=m["region_ids"],
                group_ids=m["group_ids"],
                element_ids=[e for g in sorted(owned, key=_group_order) for e in by_id[g].element_ids],
                bounds=_union([g.bounds for g in gs]),
                boundary_above_id=m["above"].id if m["above"] else None,
                boundary_below_id=m["below"].id if m["below"] else None,
                ambiguous=any(b.decision == "ambiguous" for b in adjoining + m["inner"]),
                overlaps=[],
                notes=[f"merged across {b}" for b in m["kept"]] + [f"{g} carved out: its own section" for g in carved],
            )
        )
    # Out-of-flow content no region claimed (e.g. an absolute image in a wrapper) is allocated to the
    # flow section it overlaps most, explicitly, rather than dropped.
    claims = {s.id: claim(m) for s, m in zip(sections, merged)}
    claimed = {g for c in claims.values() for g in c}
    flow = [s for s in sections if s.role == "flow"]
    for g in by_id.values():
        if g.kind != "content" or g.id in claimed or not flow:
            continue
        best = max(flow, key=lambda s: _overlap(g.bounds, s.bounds))
        best.group_ids.append(g.id)
        best.element_ids += by_id[g.id].element_ids  # content groups have no child groups
        best.notes.append(f"out-of-flow content {g.id} allocated by overlap")
        claims[best.id].add(g.id)
        claimed.add(g.id)

    _link_overlaps(sections)
    claimed = [g for c in claims.values() for g in c]
    if len(claimed) != len(set(claimed)):
        raise ValueError("a group was assigned to more than one section")
    unassigned = [g for g in by_id if g not in set(claimed)]
    if lost := [g for g in unassigned if by_id[g].kind == "content"]:
        raise ValueError(f"content groups left without a section: {lost}")
    return sections, notes, unassigned


def _unit(region_ids, group_ids, role, above=None, inner=()) -> dict:
    # inner: nested boundaries left inside this unit (an ambiguous one flags the section for review)
    return {
        "region_ids": list(region_ids), "group_ids": list(group_ids), "role": role,
        "above": above, "below": None, "kept": [], "inner": list(inner),
    }


def _subtree_ids(group_ids, by_id, stop: set[str] = frozenset()) -> set[str]:
    """Groups under group_ids, not descending into (or including) `stop` groups below the roots."""
    out, stack = set(), list(group_ids)
    while stack:
        g = stack.pop()
        out.add(g)
        stack.extend(c for c in by_id[g].child_ids if c not in stop)
    return out


def _overlap(a, b) -> tuple[float, float]:
    """(shared area, shared vertical extent): area decides, vertical extent breaks ties."""
    w = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
    h = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
    return max(w, 0) * max(h, 0), h


def _group_order(gid: str) -> int:
    return int(gid[1:])  # IDs are assigned in document pre-order


def _leftovers(gid, covered, by_id) -> list[str]:
    """Topmost groups under gid that neither are covered nor hold covered groups (e.g. a sidebar)."""
    if gid in covered:
        return []
    if not (_subtree_ids([gid], by_id) & covered):
        return [gid]
    return [x for c in by_id[gid].child_ids for x in _leftovers(c, covered, by_id)]


def _flag_hidden_content(sections: list[SectionProposal], elements: ElementSnapshot, groups: GroupSnapshot) -> None:
    """Record bands of a flow section where content exists but was captured invisible.

    Scroll-reveal effects leave elements rendered with opacity 0 (or visibility: hidden) until the page
    is scrolled, so the screenshot shows a blank area. Within each vertical stretch of the section that
    no visible content group covers, the hidden elements' extent is a band; one at least
    HIDDEN_MIN_HEIGHT tall is recorded. Only flags: sections are not split or merged.
    """
    def hidden(v) -> bool:
        # Tiny/offscreen elements are deliberate (screen-reader text), not unseen content.
        transparent = v.opacity == 0 or v.css_visibility != "visible" or v.transparent_ancestor or v.collapsed_ancestor
        return v.has_box and not v.visible and not v.tiny and not v.offscreen and transparent

    # Fixed elements are measured where they sat at capture, so their y says nothing about a section.
    spans = [(e.bounds.y, e.bounds.y + e.bounds.height) for e in elements.elements
             if e.positioning != "fixed" and hidden(e.visibility)]
    covered = sorted((g.bounds.y, g.bounds.y + g.bounds.height) for g in groups.groups
                     if g.kind == "content" and g.positioning != "fixed")
    for s in sections:
        if s.role != "flow":
            continue
        top, bottom = s.bounds.y, s.bounds.y + s.bounds.height
        # Stretches of the section between visible content.
        gaps, y = [], top
        for a, b in covered:
            if a > y:
                gaps.append((y, min(a, bottom)))
            y = max(y, b)
            if y >= bottom:
                break
        if y < bottom:
            gaps.append((y, bottom))
        for lo, hi in gaps:
            inside = [(a, b) for a, b in spans if a < hi and b > lo]
            if not inside:
                continue
            band = (max(lo, min(a for a, _ in inside)), min(hi, max(b for _, b in inside)))
            if band[1] - band[0] >= HIDDEN_MIN_HEIGHT:
                s.limitations.append(
                    SectionLimitation(kind="hidden_content", top=band[0], bottom=band[1], element_count=len(inside))
                )


def _link_overlaps(sections: list[SectionProposal]) -> None:
    for i, a in enumerate(sections):
        for b in sections[i + 1 :]:
            w = min(a.bounds.x + a.bounds.width, b.bounds.x + b.bounds.width) - max(a.bounds.x, b.bounds.x)
            h = min(a.bounds.y + a.bounds.height, b.bounds.y + b.bounds.height) - max(a.bounds.y, b.bounds.y)
            if w <= 2 or h <= 2:
                continue
            roles = {a.role, b.role}
            kind = "overlay" if "overlay" in roles else "header_over_content" if "header" in roles else "content"
            a.overlaps.append((b.id, kind))
            b.overlaps.append((a.id, kind))

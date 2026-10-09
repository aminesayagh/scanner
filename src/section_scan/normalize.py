"""Simplify the source element tree into visual groups (milestone 3).

Conservative: a wrapper is absorbed only when it adds nothing visual. Everything else is kept,
with the reasons recorded, and every source element ends up in exactly one group or in `omitted`.
"""

from dataclasses import dataclass, field

from .models import (
    Background,
    Element,
    ElementSnapshot,
    GroupEvidence,
    GroupSnapshot,
    Omitted,
    VisualGroup,
)

# Bump when grouping rules change, so old groups.json files stay identifiable.
NORMALIZER_VERSION = "1"

# Edges within this many CSS px count as the same box.
# ponytail: a fixed tolerance; move to detector.toml when milestone 5 introduces configuration.
GEOMETRY_TOLERANCE = 2.0

LANDMARK_TAGS = {"header", "footer", "main", "nav", "aside", "section", "article", "form"}
MEDIA_TAGS = {"img", "svg", "video", "audio", "picture", "canvas", "iframe", "object", "embed"}
# Kept whole as one content unit: their insides are text runs, drawing primitives or controls.
CONTENT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "button", "input", "select", "textarea", "pre", "table"}
CONTENT_TAGS |= MEDIA_TAGS


@dataclass
class _Group:
    kind: str
    primary: Element
    element_ids: list[str]
    children: list["_Group"] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def normalize(snapshot: ElementSnapshot) -> GroupSnapshot:
    by_id = {e.id: e for e in snapshot.elements}
    omitted: list[Omitted] = []

    def subtree(e: Element) -> list[str]:
        return [e.id] + [i for c in e.child_ids for i in subtree(by_id[c])]

    def has_content(e: Element) -> bool:
        return bool(e.text) or e.tag in MEDIA_TAGS or any(has_content(by_id[c]) for c in e.child_ids)

    def inline_only(e: Element) -> bool:
        # inline-block/flex/grid lay out their own boxes (e.g. cards), so they do not count as inline text.
        return e.styles.get("display") == "inline" and all(inline_only(by_id[c]) for c in e.child_ids)

    def is_content_block(e: Element) -> bool:
        if e.tag in CONTENT_TAGS:
            return True
        # Text with only inline children, e.g. <div>Read <a>more</a></div>: one run of content.
        return all(inline_only(by_id[c]) for c in e.child_ids) and has_content(e)

    def build(e: Element) -> list[_Group]:
        if e.visibility.visible and is_content_block(e):
            return [_Group("content", e, subtree(e), reasons=[f"content block <{e.tag}>"])]

        child_groups = [g for c in e.child_ids for g in build(by_id[c])]

        if not e.visibility.visible or e.box == "contents":
            # No visible box of its own (hidden, zero-size, display: contents): children move up a level.
            omitted.append(Omitted(element_id=e.id, reason="pass_through" if child_groups else "not_visible"))
            return child_groups

        if not child_groups:
            if e.text:
                # Children were already omitted with their own reasons, so the group claims only `e`.
                return [_Group("content", e, [e.id], reasons=[f"own text <{e.tag}>; children add no group"])]
            if _paints(e):
                return [_Group("decorative", e, [e.id], reasons=["paints a box with no content"])]
            omitted.append(Omitted(element_id=e.id, reason="empty_box"))
            return []

        if len(child_groups) == 1:
            keep = _reasons_to_keep(e, child_groups[0].primary)
            if not keep:
                only = child_groups[0]
                only.element_ids.append(e.id)
                if e.parent_id is None and not _reasons_to_keep(only.primary, None):
                    # The page root keeps its identity, so the root group is <body> however deep redundant
                    # wrappers go. A meaningful only-child (padding, paint, landmark) stays the root instead.
                    only.reasons.append(f"page root absorbed redundant wrapper {only.primary.id} <{only.primary.tag}>")
                    only.primary = e
                else:
                    only.reasons.append(f"absorbed wrapper {e.id} <{e.tag}>: same bounds, nothing of its own")
                return [only]
        else:
            keep = _reasons_to_keep(e, None) + [f"groups {len(child_groups)} children"]
        return [_Group("container", e, [e.id], child_groups, keep)]

    roots = build(snapshot.elements[0]) if snapshot.elements else []

    groups: list[VisualGroup] = []

    def emit(g: _Group, parent_id: str | None) -> str:
        gid = f"g{len(groups)}"
        vg = VisualGroup(
            id=gid,
            parent_id=parent_id,
            child_ids=[],
            kind=g.kind,
            primary_element_id=g.primary.id,
            element_ids=g.element_ids,
            bounds=g.primary.bounds,
            positioning=g.primary.positioning,
            evidence=_evidence(g, by_id),
            reasons=g.reasons,
        )
        groups.append(vg)
        vg.child_ids = [emit(c, gid) for c in g.children]
        return gid

    for r in roots:
        emit(r, None)
    return GroupSnapshot(
        capture_id=snapshot.capture_id, normalizer_version=NORMALIZER_VERSION, groups=groups, omitted=omitted
    )


def _alpha(css_color: str) -> float:
    """Alpha of a computed CSS colour: rgb()/rgba() legacy syntax, or modern syntax with `/ alpha`."""
    c = css_color.strip()
    if c == "transparent":
        return 0.0
    if "/" in c:
        a = c.rsplit("/", 1)[1].rstrip(") ")
        return float(a[:-1]) / 100 if a.endswith("%") else float(a)
    if c.startswith("rgba("):
        return float(c[5:-1].split(",")[3])
    return 1.0


def _own_background(e: Element) -> str:
    if e.styles.get("backgroundImage", "none") != "none":
        return "complex"
    color = e.styles.get("backgroundColor", "transparent")
    alpha = _alpha(color)
    if alpha == 0:
        return "transparent"
    return color if alpha == 1 else "complex"  # semi-transparent: blends with whatever is behind it


def _border(e: Element) -> bool:
    return any(_px(e.styles.get(f"border{side}Width", "0px")) > 0 for side in ("Top", "Right", "Bottom", "Left"))


def _padding(e: Element) -> bool:
    return any(_px(e.styles.get(f"padding{side}", "0px")) > 0 for side in ("Top", "Right", "Bottom", "Left"))


def _px(value: str) -> float:
    return float(value.removesuffix("px") or 0)


def _paints(e: Element) -> bool:
    return _own_background(e) != "transparent" or _border(e)


def _landmark(e: Element) -> str | None:
    return e.role or (e.tag if e.tag in LANDMARK_TAGS else None)


def _same_box(a: Element, b: Element) -> bool:
    ab, bb = a.bounds, b.bounds
    return (
        abs(ab.x - bb.x) <= GEOMETRY_TOLERANCE
        and abs(ab.y - bb.y) <= GEOMETRY_TOLERANCE
        and abs(ab.x + ab.width - bb.x - bb.width) <= GEOMETRY_TOLERANCE
        and abs(ab.y + ab.height - bb.y - bb.height) <= GEOMETRY_TOLERANCE
    )


def _reasons_to_keep(e: Element, only_child: Element | None) -> list[str]:
    """Why `e` matters on its own. Empty means it is a redundant wrapper around `only_child`."""
    reasons = []
    if landmark := _landmark(e):
        reasons.append(f"landmark {landmark}")
    if (bg := _own_background(e)) != "transparent":
        reasons.append(f"paints background {bg}")
    if _border(e):
        reasons.append("has a border")
    if _padding(e):
        reasons.append("has padding")
    if e.styles.get("position") in ("fixed", "sticky", "absolute"):
        reasons.append(f"positioned {e.styles['position']}")
    if "scroll_container" in e.limitations:
        reasons.append("scroll container")
    if only_child is not None and not _same_box(e, only_child):
        reasons.append("bounds differ from its only child")
    return reasons


def _evidence(g: _Group, by_id: dict[str, Element]) -> GroupEvidence:
    e = g.primary
    context_id, context = None, None
    parent = by_id.get(e.parent_id) if e.parent_id else None
    while parent is not None:
        if (bg := _own_background(parent)) != "transparent":
            context_id, context = parent.id, bg
            break
        parent = by_id.get(parent.parent_id) if parent.parent_id else None

    containers = [c.primary for c in g.children if c.kind == "container"]
    repeated = (
        len(containers) >= 2
        and len(containers) == len(g.children)
        and len({c.tag for c in containers}) == 1
        and max(c.bounds.width for c in containers) - min(c.bounds.width for c in containers) <= GEOMETRY_TOLERANCE
        and len({round(c.bounds.x) for c in containers}) >= 2
    )
    return GroupEvidence(
        landmark=_landmark(e),
        display=e.styles.get("display", ""),
        padding=_padding(e),
        border=_border(e),
        background=Background(own=_own_background(e), context_element_id=context_id, context=context),
        repeated_children=repeated,
    )

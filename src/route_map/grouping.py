"""Structural route candidates: URLs of one shape that differ in a single slot (a path segment or a query
value) become one family once that slot takes at least `min_distinct_values` different values.

URLs are partitioned by host, path depth, trailing slash, extension and query keys, so /blog/{slug} and
/blog/category/{slug} never meet. Within a partition, the URLs that agree on every slot but one are siblings
of a trie node; their varying slot is the parameter. Each URL ends in at most one family: candidates claim
their members in order: a varying path segment before a query value, the rightmost segment first, then the
larger family. A path parameter needs a constant path segment beside it, so root
pages such as /about, /contact and /privacy are never folded into a catch-all /{segment_1}.
# ponytail: one variable slot per family; nested families (/category/{a}/page/{b}) come after validation.
"""

import hashlib
import re
from collections import defaultdict

from .models import Parameter, RouteCandidate

_EXT = re.compile(r"(?<=.)(\.[A-Za-z0-9]{1,5})$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX = re.compile(r"^(?=.*\d)(?=.*[a-fA-F])[0-9a-fA-F]{8,}$")
_LOCALE = re.compile(r"^[a-z]{2}(?:[-_][a-zA-Z]{2})?$")
_SLUG = re.compile(r"^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*$")


def _shape(record: dict) -> tuple[tuple, list[str]]:
    """The partition key of a URL and its slot values (path segments, extension apart, then query values)."""
    segments = list(record["path_segments"])
    ext = ""
    if segments and (m := _EXT.search(segments[-1])):
        ext = m.group(1)
        segments[-1] = segments[-1][: -len(ext)]
    keys = tuple(k for k, _ in record["query_pairs"])
    key = (record["host"], len(segments), record["trailing_slash"], ext, keys)
    return key, segments + [v for _, v in record["query_pairs"]]


def value_type(values) -> str:
    values = list(values)
    for name, test in (("integer", str.isdigit), ("uuid", _UUID.match), ("hex", _HEX.match), ("slug", _SLUG.match)):
        if all(test(v) for v in values):
            return name
    return "string"


def pattern_of(key: tuple, slots: list, variables: dict[int, str], with_host: bool) -> str:
    """The pattern of a partition `key` with `slots` constant except `variables` ({slot index: name})."""
    host, depth, trailing, ext, keys = key
    parts = [f"{{{variables[i]}}}" if i in variables else s for i, s in enumerate(slots[:depth])]
    path = "/" + "/".join(parts) + (ext if depth else "") + ("/" if trailing else "")
    if keys:
        values = [f"{{{variables[depth + j]}}}" if depth + j in variables else v for j, v in enumerate(slots[depth:])]
        path += "?" + "&".join(f"{k}={v}" if v else k for k, v in zip(keys, values))
    return f"//{host}{path}" if with_host else path


def _query_name(key: str) -> str:
    return re.sub(r"\W+", "_", key).strip("_") or "query"


def sample_plan(members: list[tuple[str, str]], size: int, groups: dict[str, str] | None = None) -> list[str]:
    """Up to `size` member ids spread over different value shapes (type, length, digits), and over the
    outer value of a two-parameter family (`groups`: id -> e.g. its collection), the same whatever the input
    order: shapes in sorted order, taken in turn; within a shape, by a hash of the id."""
    by_shape = defaultdict(list)
    for uid, value in members:
        length = 0 if len(value) <= 8 else 1 if len(value) <= 24 else 2
        by_shape[((groups or {}).get(uid, ""), value_type([value]), length, any(c.isdigit() for c in value))].append(uid)
    queues = [sorted(ids, key=lambda i: hashlib.sha1(i.encode()).hexdigest()) for _, ids in sorted(by_shape.items())]
    picked = []
    while len(picked) < size and any(queues):
        for q in queues:
            if q and len(picked) < size:
                picked.append(q.pop(0))
    return picked


def _parameter(key: tuple, i: int, values) -> Parameter:
    depth, keys = key[1], key[4]
    if i < depth:
        return Parameter(name=f"segment_{i + 1}", location="path", position=i, type=value_type(values))
    return Parameter(name=_query_name(keys[i - depth]), location="query", position=i - depth, type=value_type(values))


def candidates(records: list[dict], min_distinct: int = 3, samples: int = 5, extra: int = 0) -> list[RouteCandidate]:
    """Families, each URL in at most one (path before query, rightmost slot first, then the larger family),
    then sibling families merged over a second path segment (see `_merge_siblings`)."""
    with_host = len({r["host"] for r in records}) > 1
    groups: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    slots_of = {}
    for r in records:
        key, slots = _shape(r)
        slots_of[r["id"]] = (key, slots)
        depth = key[1]
        for i in range(len(slots)):
            if i < depth and depth < 2:
                continue  # a path parameter needs a constant path segment beside it
            template = tuple(None if j == i else s for j, s in enumerate(slots))
            groups[(key, i, template)].append((r["id"], slots[i]))
    proposals = []
    for (key, i, template), members in groups.items():
        if len({v for _, v in members}) >= min_distinct:
            proposals.append((key, i, template, members))
    # Path before query, the rightmost segment first (URLs vary most at their end), then the larger family:
    # /s1/{segment_2} beats /{segment_1}/p1 even when more URLs share p1.
    proposals.sort(key=lambda p: (p[1] >= p[0][1], -p[1], -len(p[3]), pattern_of(p[0], p[2], {p[1]: "x"}, with_host)))
    claimed, families = set(), []
    for key, i, template, members in proposals:
        members = [(uid, v) for uid, v in members if uid not in claimed]
        if len({v for _, v in members}) < min_distinct:
            continue  # its URLs already belong to a larger family
        claimed.update(uid for uid, _ in members)
        families.append((key, i, template, members))
    out = []
    for key, i, template, members, outer in _merge_siblings(families, slots_of, claimed, min_distinct):
        values = {v for _, v in members}
        variables = {i: _parameter(key, i, values)}
        if outer is not None:
            j, by_uid, _ = outer
            variables[j] = _parameter(key, j, set(by_uid.values()))
        names = {k: p.name for k, p in variables.items()}
        out.append(RouteCandidate(
            pattern=pattern_of(key, template, names, with_host),
            parameters=[variables[k] for k in sorted(variables)],
            member_ids=sorted(uid for uid, _ in members),
            distinct_values=len(values),
            merged_families=outer[2] if outer else 0,
            sample_ids=(plan := sample_plan(members, samples + extra, outer[1] if outer else None))[:samples],
            extra_sample_ids=plan[samples:],
        ))
    return out


def _merge_siblings(families, slots_of, claimed, min_distinct):
    """Second step: one-parameter families that differ only in one other path segment, at least
    `min_distinct` of them, become one family with two parameters (/collections/men/products/{x},
    /collections/women/products/{x}, ... -> /collections/{segment_2}/products/{segment_4}). URLs of the same
    shape left out of every family (a collection with one product) join it.
    Language prefixes stay apart (/en/..., /fr/... are separate routes in this version), and a pattern keeps
    at least one constant path segment.
    Returns (key, variable slot, template, members, outer) where outer is None or (slot, {id: its value},
    how many families were merged).
    """
    siblings = defaultdict(list)
    for n, (key, i, template, _) in enumerate(families):
        if i >= key[1]:
            continue  # path parameters only
        for j in range(key[1]):
            if j != i and template[j] is not None:
                siblings[(key, i, j, template[:j] + (None,) + template[j + 1:])].append(n)
    loose = defaultdict(list)
    for uid, (key, slots) in slots_of.items():
        if uid not in claimed:
            loose[key].append((uid, slots))
    used, merged = set(), []
    for (key, i, j, outer), idx in sorted(siblings.items(), key=lambda s: (-len(s[1]), -s[0][2], str(s[0]))):
        idx = [n for n in idx if n not in used]
        names = {families[n][2][j] for n in idx}
        if len(names) < min_distinct or all(_LOCALE.match(v) for v in names):
            continue
        if not any(s is not None for k, s in enumerate(outer[: key[1]]) if k != i):
            continue  # the merged pattern would have no constant path segment
        used.update(idx)
        members, by_uid = [], {}
        for n in idx:
            for uid, v in families[n][3]:
                members.append((uid, v))
                by_uid[uid] = families[n][2][j]
        for uid, slots in loose[key]:
            if uid not in by_uid and all(s == slots[k] for k, s in enumerate(outer) if s is not None and k != i):
                members.append((uid, slots[i]))
                by_uid[uid] = slots[j]
        merged.append((key, i, outer, members, (j, by_uid, len(idx))))
    for n, (key, i, template, members) in enumerate(families):
        if n not in used:
            merged.append((key, i, template, members, None))
    return merged

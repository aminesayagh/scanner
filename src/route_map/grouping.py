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


def pattern_of(key: tuple, slots: list, variable: int, name: str, with_host: bool) -> str:
    host, depth, trailing, ext, keys = key
    parts = [f"{{{name}}}" if i == variable else s for i, s in enumerate(slots[:depth])]
    path = "/" + "/".join(parts) + (ext if depth else "") + ("/" if trailing else "")
    if keys:
        values = [f"{{{name}}}" if depth + j == variable else v for j, v in enumerate(slots[depth:])]
        path += "?" + "&".join(f"{k}={v}" if v else k for k, v in zip(keys, values))
    return f"//{host}{path}" if with_host else path


def _query_name(key: str) -> str:
    return re.sub(r"\W+", "_", key).strip("_") or "query"


def sample_plan(members: list[tuple[str, str]], size: int) -> list[str]:
    """Up to `size` member ids spread over different value shapes (type, length, digits), the same whatever
    the input order: shapes in sorted order, taken in turn; within a shape, by a hash of the id."""
    by_shape = defaultdict(list)
    for uid, value in members:
        length = 0 if len(value) <= 8 else 1 if len(value) <= 24 else 2
        by_shape[(value_type([value]), length, any(c.isdigit() for c in value))].append(uid)
    queues = [sorted(ids, key=lambda i: hashlib.sha1(i.encode()).hexdigest()) for _, ids in sorted(by_shape.items())]
    picked = []
    while len(picked) < size and any(queues):
        for q in queues:
            if q and len(picked) < size:
                picked.append(q.pop(0))
    return picked


def candidates(records: list[dict], min_distinct: int = 3, samples: int = 5, extra: int = 0) -> list[RouteCandidate]:
    """Families, each URL in at most one (path before query, rightmost slot first, then the larger family)."""
    with_host = len({r["host"] for r in records}) > 1
    groups: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    for r in records:
        key, slots = _shape(r)
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
    proposals.sort(key=lambda p: (p[1] >= p[0][1], -p[1], -len(p[3]), pattern_of(p[0], p[2], p[1], "x", with_host)))
    claimed, out = set(), []
    for key, i, template, members in proposals:
        members = [(uid, v) for uid, v in members if uid not in claimed]
        values = {v for _, v in members}
        if len(values) < min_distinct:
            continue  # its URLs already belong to a larger family
        depth, keys = key[1], key[4]
        if i < depth:
            parameter = Parameter(name=f"segment_{i + 1}", location="path", position=i, type=value_type(values))
        else:
            parameter = Parameter(name=_query_name(keys[i - depth]), location="query", position=i - depth,
                                  type=value_type(values))
        claimed.update(uid for uid, _ in members)
        out.append(RouteCandidate(
            pattern=pattern_of(key, template, i, parameter.name, with_host),
            parameter=parameter,
            member_ids=sorted(uid for uid, _ in members),
            distinct_values=len(values),
            sample_ids=(plan := sample_plan(members, samples + extra))[:samples],
            extra_sample_ids=plan[samples:],
        ))
    return out

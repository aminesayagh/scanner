"""Views of a route map: the URL prefix hierarchy, the links observed between routes, a readable summary.

The prefix tree organizes routes by URL only: a node no route sits on (e.g. /product when only
/product/{product_slug} exists) is marked synthetic, not a discovered page. Observed links are kept apart
from it: a category is never said to hold a product without a link actually seen between them.
"""

from collections import Counter, defaultdict

from .models import PageObservation, RouteMap, RouteRecord, URLRecord
from .normalization import Rejected, normalize


def prefix_tree(routes: list[RouteRecord]) -> dict:
    root = {"segment": "", "path": "/", "route_ids": [], "synthetic": True, "children": {}}
    for route in routes:
        path = route.pattern.split("?")[0]
        node = root
        for segment in [s for s in path.split("/") if s]:
            child_path = node["path"].rstrip("/") + "/" + segment
            node = node["children"].setdefault(segment, {"segment": segment, "path": child_path, "route_ids": [],
                                                         "synthetic": True, "children": {}})
        node["route_ids"].append(route.id)
        node["synthetic"] = False

    def finish(node):
        node["route_ids"].sort()
        node["children"] = [finish(c) for _, c in sorted(node["children"].items())]
        return node

    return finish(root)


def link_edges(records: list[URLRecord], observations: list[PageObservation], tracking: list[str]) -> list[dict]:
    """Route-to-route links seen on fetched pages: how many links, from how many pages."""
    route_of_id = {r.id: r.route_id for r in records}
    route_of_key = {r.comparison_key: r.route_id for r in records}
    links, pages = Counter(), defaultdict(set)
    for o in observations:
        source = route_of_id.get(o.url_id)
        if not source:
            continue
        for link in o.links:
            try:
                target = route_of_key.get(normalize(link, tracking=tracking)["comparison_key"])
            except Rejected:
                continue
            if target:
                links[(source, target)] += 1
                pages[(source, target)].add(o.url_id)
    return [{"from": a, "to": b, "links": n, "pages": len(pages[(a, b)])} for (a, b), n in sorted(links.items())]


def summary(result: RouteMap) -> str:
    """A readable Markdown summary of a route map."""
    report = result.crawl_report
    families = [r for r in result.routes if r.status != "individual"]
    lines = [f"# Route map of {result.seed or 'an inventory'}", "",
             f"{len(result.urls)} URLs, {len(result.routes)} routes ({len(families)} families), {len(result.templates)} templates. "
             f"{report.discovery_html} discovery pages and {report.validation_html} sample pages fetched, "
             f"{report.requests_used} requests in all.", ""]
    if report.stop_reasons:
        lines += ["**Partial map:** " + "; ".join(report.stop_reasons) + ".", ""]
    lines += ["| Route | Status | Role | URLs | Samples | Examples |", "| --- | --- | --- | --- | --- | --- |"]
    for r in sorted(result.routes, key=lambda r: (r.status == "individual", -r.discovered_url_count, r.pattern)):
        status = r.status + (f" ({r.template})" if r.template else "")
        role = r.role + ("" if r.role_certainty == "supported" else f" ({r.role_certainty})" if r.role != "unknown" else "")
        examples = ", ".join(f"`{e}`" for e in r.examples[:2])
        lines.append(f"| `{r.pattern}` | {status} | {role} | {r.discovered_url_count} | {r.successful_sample_count} | {examples} |")
    exceptions = [(r.pattern, r.excluded_literals) for r in result.routes if r.excluded_literals]
    if exceptions:
        lines += ["", "## Exceptions", ""] + [f"- `{p}` leaves out " + ", ".join(f"`{x}`" for x in xs) for p, xs in exceptions]
    if result.templates:
        lines += ["", "## Templates", "", "| Representative | Status | Checked | Inferred | Routes |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| `{t.representative}` | {t.status} | {t.checked_page_count} | {t.inferred_page_count} | {', '.join(t.route_ids)} |"
                  for t in result.templates]
    if report.limitations:
        lines += ["", "## Limitations", ""] + [f"- {x}" for x in report.limitations]
    return "\n".join(lines) + "\n"

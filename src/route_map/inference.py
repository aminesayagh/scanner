"""The offline entry point: a URL inventory (and, later, page observations) in, a route map out.

No network access here, so grouping can be evaluated on fixture inventories. The result depends only on
the set of URLs and the configuration, not on their order.
"""

from urllib.parse import urlsplit

from .grouping import candidates as find_candidates
from .models import Config, CrawlReport, Evidence, PageObservation, RouteMap, RouteRecord, URLRecord
from .normalization import Rejected, normalize, url_id


def records_from(urls: list[tuple[str, str]], config: Config, seed: str | None = None) -> tuple[list[URLRecord], list[str]]:
    """One record per URL identity (url, source pairs; duplicates become aliases), and the rejected URLs:
    not HTTP(S), or on another host than the seed's (exact host match)."""
    allowed = normalize(seed)["crawl_url"] if seed else None
    allowed_host = urlsplit(allowed).netloc if allowed else None
    by_key: dict[str, dict] = {}
    rejected = []
    for url, source in sorted(set(urls)):
        try:
            fields = normalize(url.strip(), tracking=config.tracking_parameters)
        except Rejected:
            rejected.append(url)
            continue
        host = urlsplit(fields["crawl_url"]).netloc
        if allowed_host and host != allowed_host:
            rejected.append(url)
            continue
        record = by_key.get(fields["comparison_key"])
        if record is None:
            record = by_key[fields["comparison_key"]] = {**fields, "id": url_id(fields["comparison_key"]), "host": host,
                                                         "sources": [], "aliases": []}
        elif url != record["original_url"] and url not in record["aliases"]:
            record["aliases"].append(url)
        if source not in record["sources"]:
            record["sources"].append(source)
    return [URLRecord(**r) for r in sorted(by_key.values(), key=lambda r: r["comparison_key"])], sorted(set(rejected))


def infer(
    urls: list[tuple[str, str]], config: Config | None = None, seed: str | None = None,
    observations: list[PageObservation] | None = None,
) -> RouteMap:
    """Route families from an inventory. Without page observations every family is provisional (a
    structural hypothesis); validation on samples is the next stage. URLs in no family stay individual."""
    config = config or Config()
    records, rejected = records_from(urls, config, seed)
    plain = [r.model_dump() | {"host": urlsplit(r.crawl_url).netloc} for r in records]
    found = find_candidates(plain, config.min_distinct_values, config.samples_per_family)
    by_id = {r.id: r for r in records}

    def path(uid):
        u = urlsplit(by_id[uid].crawl_url)
        return u.path + (f"?{u.query}" if u.query else "")

    drafts = []
    for c in found:
        drafts.append((c.pattern, RouteRecord(
            id="", pattern=c.pattern, status="provisional", parameters=[c.parameter],
            discovered_url_count=len(c.member_ids), inferred_unsampled_url_count=len(c.member_ids),
            examples=[path(u) for u in c.sample_ids[:3]], sample_ids=c.sample_ids,
            evidence=[Evidence(kind="structure", value=f"{c.distinct_values} distinct values in {c.parameter.name}")],
        ), c.member_ids))
    assigned = {u for *_, members in drafts for u in members}
    for r in records:
        if r.id not in assigned:
            drafts.append((path(r.id), RouteRecord(id="", pattern=path(r.id), status="individual",
                                                   discovered_url_count=1, examples=[path(r.id)]), [r.id]))
    routes = []
    for n, (_, route, members) in enumerate(sorted(drafts, key=lambda d: (d[0], d[1].status)), 1):
        route.id = f"route_{n:03d}"
        routes.append(route)
        for uid in members:
            by_id[uid].route_id = route.id
    limitations = []
    if any(r.hash_route for r in records):
        limitations.append("hash-router URLs (#/ or #!) found: client-side routes are not followed in this version")
    report = CrawlReport(budgets=config, discovered_urls=len(records), rejected_urls=rejected, limitations=limitations)
    return RouteMap(seed=seed, routes=routes, urls=records, observations=observations or [], crawl_report=report)

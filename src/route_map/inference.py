"""The offline entry point: a URL inventory (and, later, page observations) in, a route map out.

No network access here, so grouping can be evaluated on fixture inventories. The result depends only on
the set of URLs and the configuration, not on their order.
"""

from urllib.parse import urlsplit

from .grouping import candidates as find_candidates
from .models import Config, CrawlReport, Evidence, PageObservation, RouteMap, RouteRecord, URLRecord
from .export import link_edges, prefix_tree
from .features import ROLE_TYPES
from .normalization import Rejected, normalize, url_id
from .roles import assign_roles
from .validation import judge, unusable_reason


def template_evidence(sample: list[str], obs: dict, usable: int) -> list[Evidence]:
    """What a template's samples share: structure, structured data, functional signals, landmarks."""
    n = len(sample)
    out = [Evidence(kind="main_structure", value="similar", sample_support=f"{n}/{usable}")]
    for t in sorted(set.intersection(*(set(obs[u].structured_data_types) for u in sample)) & ROLE_TYPES):
        out.append(Evidence(kind="structured_data", value=t, sample_support=f"{n}/{n}"))
    for signal in sorted(set.intersection(*(set(obs[u].signals) for u in sample))):
        out.append(Evidence(kind="signal", value=signal, sample_support=f"{n}/{n}"))
    if weak := sum(obs[u].main_source == "heuristic" for u in sample):
        out.append(Evidence(kind="main_content", value="no main landmark: body without header, nav, footer, aside",
                            sample_support=f"{weak}/{n}"))
    if moved := sum(bool(obs[u].canonical and obs[u].canonical.rstrip("/") != (obs[u].final_url or "").rstrip("/")) for u in sample):
        out.append(Evidence(kind="canonical", value="points to another URL", sample_support=f"{moved}/{n}"))
    return out


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


def families(records: list[URLRecord], config: Config):
    """The structural route candidates of an inventory (also used by the crawler to plan samples)."""
    plain = [r.model_dump() | {"host": urlsplit(r.crawl_url).netloc} for r in records]
    return find_candidates(plain, config.min_distinct_values, config.samples_per_family, config.extra_samples)


def infer(
    urls: list[tuple[str, str]], config: Config | None = None, seed: str | None = None,
    observations: list[PageObservation] | None = None, redirects: dict[str, str] | None = None,
    report: dict | None = None,
) -> RouteMap:
    """Route families from an inventory. With page observations, each family is judged on its sampled pages
    (see validation.py); without, every family stays provisional. URLs in no family stay individual."""
    config = config or Config()
    records, rejected = records_from(urls, config, seed)
    moved = {}
    for source, target in (redirects or {}).items():
        try:
            moved[normalize(source, tracking=config.tracking_parameters)["comparison_key"]] = target
        except Rejected:
            pass
    for r in records:
        r.redirect_to = moved.get(r.comparison_key)  # evidence only: a redirect does not set a role
    found = families(records, config)
    by_id = {r.id: r for r in records}
    obs = {o.url_id: o for o in observations or []}

    def path(uid):
        u = urlsplit(by_id[uid].crawl_url)
        return u.path + (f"?{u.query}" if u.query else "")

    drafts: list[dict] = []  # route, its sort key, assigned and ambiguous URL ids, keys of its exception routes

    def add(key, route, assigned, ambiguous=(), assignment="assigned", exceptions=()):
        drafts.append({"key": key, "route": route, "assigned": list(assigned), "ambiguous": list(ambiguous),
                       "assignment": assignment, "exceptions": list(exceptions)})

    def single(uid, evidence=()):
        add((path(uid), "", "individual", uid), RouteRecord(
            id="", pattern=path(uid), status="individual", discovered_url_count=1, examples=[path(uid)],
            successful_sample_count=int(uid in obs and unusable_reason(obs[uid], by_id[uid]) is None),
            sample_ids=[uid] if uid in obs else [],
            evidence=list(evidence)), [uid])

    for c in found:
        outcome = judge(c.member_ids, obs, by_id, config)
        evidence = [Evidence(kind="structure", value=f"{c.distinct_values} distinct values in {c.parameters[-1].name}")]
        if c.merged_families:
            evidence.append(Evidence(kind="structure", value=f"{c.merged_families} sibling families merged over {c.parameters[0].name}"))
        evidence += [Evidence(kind="unusable_sample", value=f"{why}: {path(u)}") for u, why in sorted(outcome.unusable.items())]
        common = dict(pattern=c.pattern)  # each route gets its own parameter copy: roles rename them
        if outcome.kind == "provisional":
            add((c.pattern, "", "provisional"), RouteRecord(
                id="", status="provisional", parameters=[p.model_copy() for p in c.parameters], discovered_url_count=len(c.member_ids),
                successful_sample_count=len(outcome.loose), inferred_unsampled_url_count=len(c.member_ids), examples=[path(u) for u in c.sample_ids[:3]],
                sample_ids=c.sample_ids, evidence=evidence, **common), c.member_ids)
            continue
        if outcome.kind == "unresolved":
            rest = [u for u in c.member_ids if u not in outcome.loose]
            add((c.pattern, "", "unresolved"), RouteRecord(
                id="", status="unresolved", parameters=[p.model_copy() for p in c.parameters], discovered_url_count=len(rest), inferred_unsampled_url_count=len(rest),
                examples=[path(u) for u in c.sample_ids[:3]], sample_ids=c.sample_ids,
                evidence=evidence + [Evidence(kind="main_structure", value="samples disagree",
                                              sample_support=f"0/{len(outcome.loose)}")], **common),
                rest, assignment="unresolved")
            for u in outcome.loose:
                single(u, [Evidence(kind="family", value=f"{c.pattern} unresolved: its samples disagree")])
            continue
        sampled = {u for t in outcome.templates for u in t}
        for u in outcome.exceptions:
            single(u, [Evidence(kind="exception", value=f"its structure differs from the other samples of {c.pattern}")])
        exception_keys = [(path(u), "", "individual", u) for u in outcome.exceptions]
        usable_count = sum(len(t) for t in outcome.templates) + len(outcome.exceptions)
        others = [u for u in c.member_ids if u not in sampled and u not in outcome.exceptions]
        mixed = len(outcome.templates) > 1
        for k, t in enumerate(outcome.templates):
            assigned = t if mixed else t + others
            add((c.pattern, chr(97 + k) if mixed else "", "validated_on_sample"), RouteRecord(
                id="", status="validated_on_sample", parameters=[p.model_copy() for p in c.parameters], template=chr(97 + k) if mixed else None,
                discovered_url_count=len(assigned), successful_sample_count=len(t),
                inferred_unsampled_url_count=len(assigned) - len(t), ambiguous_url_count=len(others) if mixed else 0,
                examples=[path(u) for u in t[:3]], sample_ids=t,
                evidence=evidence + template_evidence(t, obs, usable_count),
                excluded_literals=[path(u) for u in outcome.exceptions], **common),
                assigned, others if mixed else (), exceptions=exception_keys)

    placed = {u for d in drafts for u in d["assigned"] + d["ambiguous"]}
    for r in records:
        if r.id not in placed:
            single(r.id)
    drafts.sort(key=lambda d: d["key"])
    ids = {}
    for n, d in enumerate(drafts, 1):
        d["route"].id = ids[d["key"]] = f"route_{n:03d}"
    candidates_of: dict[str, list[str]] = {}
    for d in drafts:
        d["route"].exception_route_ids = sorted(ids[k] for k in d["exceptions"])
        for uid in d["assigned"]:
            by_id[uid].route_id, by_id[uid].assignment = d["route"].id, d["assignment"]
        for uid in d["ambiguous"]:
            candidates_of.setdefault(uid, []).append(d["route"].id)
    for uid, route_ids in candidates_of.items():
        by_id[uid].route_id, by_id[uid].assignment, by_id[uid].candidate_route_ids = None, "ambiguous", sorted(route_ids)
    routes = [d["route"] for d in drafts]
    limitations = []
    if any(r.hash_route for r in records):
        limitations.append("hash-router URLs (#/ or #!) found: client-side routes are not followed in this version")
    found_report = dict(report or {})
    report = CrawlReport(**{**found_report, "budgets": config, "discovered_urls": len(records), "rejected_urls": rejected,
                            "limitations": found_report.get("limitations", []) + limitations})
    assign_roles(routes, records, observations or [], config.tracking_parameters)
    return RouteMap(seed=seed, routes=routes, urls=records, observations=observations or [], crawl_report=report,
                    tree=prefix_tree(routes), links=link_edges(records, observations or [], config.tracking_parameters))

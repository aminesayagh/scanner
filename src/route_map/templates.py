"""Template inventory: the checked pages of a whole site grouped by skeleton, whatever their URL.

A route groups URLs by pattern (/product/{slug}); a template groups fetched pages by their main-content
skeleton (features.py, rich-text bodies collapsed). The two are independent: a template can span routes (the
home page and a landing page built on one layout, two blog posts too few to make a family) and a route can
hold several templates (a provisional family whose few checked pages differ). Only usable pages are placed,
with the same clustering as validation.py; unchecked URLs have no template. Each template names one representative,
the member closest on average to the others, so it is screenshotted once. No network access here.
"""

from .features import ROLE_TYPES
from .models import Config, Evidence, PageObservation, RouteRecord, TemplateRecord, URLRecord
from .normalization import display_path
from .validation import jaccard, site_clusters, usable_pages


def inventory(records: list[URLRecord], routes: list[RouteRecord], observations: list[PageObservation],
              config: Config) -> list[TemplateRecord]:
    """The site's templates, largest first; sets `template_id` on each placed URL record."""
    by_id = {r.id: r for r in records}
    usable = usable_pages(observations, by_id)
    features = {u: set(o.features) for u, o in usable.items()}
    # ponytail: clustering compares each page with every member of every template, O(pages² × templates);
    # an inverted index on skeleton paths is the upgrade if crawls grow to many thousands of pages.
    groups = site_clusters(usable, config)

    def closeness(u, members):  # rounded so that true ties fall to the path, whatever the float order
        return round(sum(jaccard(features[u], features[v]) for v in members if v != u) / max(1, len(members) - 1), 9)

    drafts = []
    for members in groups:
        members = sorted(members)
        rep = min(members, key=lambda u: (-closeness(u, members), display_path(by_id[u].crawl_url), u))
        n = len(members)
        evidence = [Evidence(kind="skeleton", value="similar", sample_support=f"{n}/{n}")] if n > 1 else []
        if k := sum(usable[u].rich_text_blocks > 0 for u in members):
            evidence.append(Evidence(kind="rich_text", value="rich-text body collapsed", sample_support=f"{k}/{n}"))
        for t in sorted(set.intersection(*(set(usable[u].structured_data_types) for u in members)) & ROLE_TYPES):
            evidence.append(Evidence(kind="structured_data", value=t, sample_support=f"{n}/{n}"))
        drafts.append((rep, TemplateRecord(
            id="", representative=display_path(by_id[rep].crawl_url), status="repeated" if n > 1 else "single",
            checked_page_count=n, route_ids=sorted({by_id[u].route_id for u in members} - {None}), page_ids=members,
            evidence=evidence)))
    drafts.sort(key=lambda d: (-d[1].checked_page_count, d[1].representative, d[0]))
    templates = [t for _, t in drafts]
    for number, t in enumerate(templates, 1):
        t.id = f"template_{number:03d}"
        for u in t.page_ids:
            by_id[u].template_id = t.id
    for route in routes:  # a validated route's unsampled pages follow its samples, if they share one template
        homes = {by_id[u].template_id for u in route.sample_ids if u in usable}
        if route.status == "validated_on_sample" and len(homes) == 1:
            next(t for t in templates if t.id in homes).inferred_page_count += route.inferred_unsampled_url_count
    return templates

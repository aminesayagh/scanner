"""Page roles from small, inspectable rules, then parameter names that follow the role.

Evidence comes from a route's usable samples (structured data, functional signals, links to other routes)
and, weakly, from words in its URL. A rule is "supported" when at least 80% of the samples show its cues;
URL words alone, or cues on at least half the samples, give a "weak" role. Two supported roles that contradict each other give
"unknown" with the reason. Unresolved routes stay unknown, and a role is never read from the spelling of a
URL that was not observed beyond the weak URL-word rule.

Rules (cues per sample):
- home: the site root.
- product_detail: Product/ProductGroup JSON-LD and a purchase control. The type alone is weak.
- product_listing: a listing grid (or CollectionPage/ItemList) whose links reach product_detail pages;
  embedded Product markup alone does not make a page a product page.
- article: Article/BlogPosting/NewsArticle JSON-LD and article metadata. Either alone is weak.
- article_listing: a listing grid whose links reach article pages.
- search_results: SearchResultsPage JSON-LD, or a search URL (a q, s, query or search parameter, or a /search
  path) with a listing. It wins over product_listing and article_listing.
- contact: ContactPage JSON-LD or a contact form (a textarea and an email field) in the main content.
- authentication: a password field in the main content.
- cart: a checkout control in the main content of a page whose URL says cart, basket or bag.
- checkout / informational: URL words (weak), or AboutPage/FAQPage JSON-LD for informational.
"""

from .models import Evidence, PageObservation, RouteRecord, URLRecord
from .normalization import Rejected, normalize
from .validation import unusable_reason

SUPPORTED_SHARE = 0.8
WORDS = {
    "product_detail": {"product", "products", "item", "items", "p", "dp"},
    "product_listing": {"collection", "collections", "category", "categories", "catalog", "catalogue", "shop", "c"},
    "article": {"blog", "blogs", "news", "article", "articles", "post", "posts", "stories", "journal"},
    "search_results": {"search", "find"},
    "contact": {"contact", "contact-us", "contacts", "kontakt"},
    "authentication": {"login", "signin", "sign-in", "register", "signup", "sign-up", "auth"},
    "cart": {"cart", "basket", "bag"},
    "checkout": {"checkout", "checkouts"},
    "informational": {"about", "about-us", "faq", "faqs", "privacy", "privacy-policy", "terms", "policy", "policies",
                      "legal", "shipping", "returns", "imprint", "pages", "help"},
}
SEARCH_KEYS = {"q", "s", "query", "search", "keyword", "keywords"}
# A family's variable segment after these words names the detail page; the bare word page is the listing.
LISTING_OF = {"product_detail": "product_listing", "article": "article_listing"}
PARAMETER_NAMES = {
    ("product_detail", "slug"): "product_slug", ("product_detail", "string"): "product_slug",
    ("product_detail", "integer"): "product_id", ("product_detail", "uuid"): "product_id", ("product_detail", "hex"): "product_id",
    ("article", "slug"): "article_slug", ("article", "string"): "article_slug",
    ("article", "integer"): "article_id", ("article", "uuid"): "article_id", ("article", "hex"): "article_id",
    ("product_listing", "slug"): "category_slug", ("article_listing", "slug"): "category_slug",
}


def _literals(route: RouteRecord) -> tuple[list[str], list[str]]:
    """The constant path segments and the query keys of a route's pattern."""
    path, _, query = route.pattern.partition("?")
    segments = [s for s in path.split("/") if s and not s.startswith("{")]
    keys = [pair.split("=", 1)[0] for pair in query.split("&") if pair]
    return [s.lower() for s in segments], [k.lower() for k in keys]


def url_word_role(route: RouteRecord) -> str | None:
    segments, keys = _literals(route)
    if route.pattern.split("?")[0] == "/" and not keys:
        return "home"
    if SEARCH_KEYS & set(keys):
        return "search_results"
    for segment in reversed(segments):  # the most specific word wins: /blog/category/{x} is about the category
        for role, words in WORDS.items():
            if segment in words or segment.split(".")[0] in words:
                bare = not route.parameters and route.pattern.rstrip("/").lower().endswith("/" + segment)
                return LISTING_OF.get(role, role) if bare else role
    return None


def _share(samples: list[PageObservation], test) -> float:
    return sum(bool(test(o)) for o in samples) / len(samples) if samples else 0.0


def _types(o: PageObservation) -> set[str]:
    return set(o.structured_data_types)


def assign_roles(routes: list[RouteRecord], records: list[URLRecord], observations: list[PageObservation],
                 tracking: list[str]) -> None:
    """Sets role, role_certainty and role evidence on every route, then renames parameters after the role."""
    obs = {o.url_id: o for o in observations}
    by_id = {r.id: r for r in records}
    route_of_key = {r.comparison_key: r.route_id for r in records if r.route_id}

    def samples(route):
        return [obs[u] for u in route.sample_ids if u in obs and u in by_id and unusable_reason(obs[u], by_id[u]) is None]

    def link_routes(o):
        out = []
        for link in o.links:
            try:
                out.append(route_of_key.get(normalize(link, tracking=tracking)["comparison_key"]))
            except Rejected:
                pass
        return out

    structural: dict[str, dict[str, float]] = {}
    for route in routes:
        s = samples(route)
        _, keys = _literals(route)
        cues = {
            "product_detail": _share(s, lambda o: _types(o) & {"Product", "ProductGroup"} and "purchase" in o.signals),
            "article": _share(s, lambda o: _types(o) & {"Article", "BlogPosting", "NewsArticle"} and "article_meta" in o.signals),
            "search_results": _share(s, lambda o: "SearchResultsPage" in _types(o) or (
                (SEARCH_KEYS & set(keys) or url_word_role(route) == "search_results") and "listing" in o.signals)),
            "contact": _share(s, lambda o: "ContactPage" in _types(o) or "contact_form" in o.signals),
            "authentication": _share(s, lambda o: "password" in o.signals),
            "informational": _share(s, lambda o: _types(o) & {"AboutPage", "FAQPage"}),
        }
        if url_word_role(route) == "cart":
            cues["cart"] = _share(s, lambda o: "checkout" in o.signals)
        weak = {
            "product_detail": _share(s, lambda o: _types(o) & {"Product", "ProductGroup"}),
            "article": _share(s, lambda o: _types(o) & {"Article", "BlogPosting", "NewsArticle"} or "article_meta" in o.signals),
        }
        structural[route.id] = {"cues": cues, "weak": weak, "n": len(s)}

    def decide(route, listing_cues):
        info = structural[route.id]
        n = info["n"]
        if route.status == "unresolved":
            return "unknown", "unknown", [Evidence(kind="role", value="unknown: the family's samples disagree")]
        cues = {**info["cues"], **listing_cues}
        supported = sorted(role for role, share in cues.items() if n and share >= SUPPORTED_SHARE)
        if "product_detail" in supported and "product_listing" in supported:
            supported.remove("product_listing")  # a product page with a related-products grid
        if "search_results" in supported:  # search results list products or articles
            supported = [r for r in supported if r not in ("product_listing", "article_listing")]
        word = url_word_role(route)
        if word == "home":
            return "home", "supported", [Evidence(kind="role", value="home: the site root")]
        if len(supported) == 1:
            role = supported[0]
            return role, "supported", [Evidence(kind="role", value=f"{role}: page cues",
                                                sample_support=f"{round(cues[role] * n)}/{n}")]
        if len(supported) > 1:
            return "unknown", "unknown", [Evidence(kind="role", value="unknown: conflicting cues for " + ", ".join(supported))]
        # Cues on at least half the samples outweigh URL words; rarer ones (one contact page among five) do not.
        partial = sorted((share, role) for role, share in {**cues, **info["weak"]}.items() if share >= 0.5)
        if partial:
            share, role = partial[-1]
            return role, "weak", [Evidence(kind="role", value=f"{role}: partial page cues", sample_support=f"{round(share * n)}/{n}")]
        if word:
            return word, "weak", [Evidence(kind="role", value=f"{word}: URL words only")]
        return "unknown", "unknown", []

    # First the roles that need no other route, then listings (they are known by what their links reach).
    first = {r.id: decide(r, {}) for r in routes}
    role_of = {rid: role for rid, (role, certainty, _) in first.items() if certainty != "unknown"}
    for route in routes:
        s = samples(route)
        listing_cues = {}
        if s:
            def reaches(o, target):
                hits = [rid for rid in link_routes(o) if role_of.get(rid) == target and rid != route.id]
                return len(hits) >= 3 and ("listing" in o.signals or _types(o) & {"CollectionPage", "ItemList"})
            listing_cues = {"product_listing": _share(s, lambda o: reaches(o, "product_detail")),
                            "article_listing": _share(s, lambda o: reaches(o, "article"))}
        role, certainty, evidence = decide(route, listing_cues) if any(listing_cues.values()) else first[route.id]
        route.role, route.role_certainty = role, certainty
        route.evidence = route.evidence + evidence
        for p in route.parameters:
            if p.location == "path" and (name := PARAMETER_NAMES.get((role, p.type))) and certainty == "supported":
                route.pattern = route.pattern.replace("{" + p.name + "}", "{" + name + "}")
                p.name = name

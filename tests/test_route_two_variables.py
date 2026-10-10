"""Families with two parameters: sibling families merged over a second path segment."""

import re

from route_map.inference import infer
from route_map.models import Config

from route_site import Site, html, urlset
from test_route_validation import product, seen

BASE = "https://shop.test"
COLLECTIONS = ("men", "women", "kids", "sale")


def run(urls, observations=()):
    return infer([(u, "inventory") for u in urls], Config(), seed=BASE + "/", observations=list(observations))


def shop_urls():
    urls = [f"{BASE}/collections/{c}/products/{c}-item-{n}" for c in COLLECTIONS for n in range(4)]
    return urls + [f"{BASE}/collections/bags/products/the-bucket"]  # a collection with one product


def test_sibling_families_merge_into_one_family_with_two_parameters():
    routes = run(shop_urls()).routes
    assert [(r.pattern, r.discovered_url_count) for r in routes] == [("/collections/{segment_2}/products/{segment_4}", 17)]
    assert [p.position for p in routes[0].parameters] == [1, 3]
    assert any("4 sibling families merged over segment_2" in e.value for e in routes[0].evidence)


def test_two_sibling_families_are_not_enough():
    urls = [f"{BASE}/collections/{c}/products/{c}-item-{n}" for c in ("men", "women") for n in range(4)]
    assert {r.pattern for r in run(urls).routes} == {"/collections/men/products/{segment_4}", "/collections/women/products/{segment_4}"}


def test_a_merge_that_leaves_no_constant_segment_is_refused():
    urls = [f"{BASE}/{a}/{b}{n}" for a in ("x1", "x2", "x3") for b in ("p",) for n in range(4)]
    assert all(len(r.parameters) == 1 for r in run(urls).routes)


def test_samples_spread_over_the_outer_values():
    result = run(shop_urls())
    url_of = {u.id: u.crawl_url for u in result.urls}
    outer = [re.search(r"/collections/([^/]+)/", url_of[u]).group(1) for u in result.routes[0].sample_ids]
    assert sorted(outer) == ["bags", "kids", "men", "sale", "women"]  # five samples, one per collection


def test_parameters_are_named_by_role_and_by_the_word_before_them():
    urls = shop_urls()
    obs = [seen(u, product(n)) for n, u in enumerate(urls[::4][:4])]
    family = run(urls, obs).routes[0]
    assert family.status == "validated_on_sample" and family.pattern == "/collections/{category_slug}/products/{product_slug}"


def test_crawl_validates_a_shopify_like_shop_as_one_product_family(tmp_path):
    def routes(base):
        paths = [f"/collections/c{c}/products/p{c}-{n}" for c in range(10) for n in range(6)]
        pages = {"/robots.txt": ("text/plain", "Sitemap: /sitemap.xml\n"), "/sitemap.xml": ("application/xml", urlset(base, paths)),
                 "/": ("text/html", html("Shop", []))}
        pages.update({p: ("text/html", product(n)) for n, p in enumerate(paths)})
        return pages

    data = Site(routes).map(tmp_path, "--max-html-pages", "12")
    families = [r for r in data["routes"] if r["status"] != "individual"]
    assert [(f["pattern"], f["status"], f["discovered_url_count"]) for f in families] == [
        ("/collections/{category_slug}/products/{product_slug}", "validated_on_sample", 60)]

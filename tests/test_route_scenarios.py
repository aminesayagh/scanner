"""The plan's milestone-6 scenarios not covered elsewhere, and reproducibility across response order."""

import re

from route_map.inference import infer
from route_map.models import Config

from route_site import Site, html, redirect, urlset
from test_route_validation import ARTICLE, COMPARE, PRODUCT, product, seen

BASE = "https://shop.test"
OTHER_PRODUCT = """<html><head><title>{name}</title><script type="application/ld+json">{{"@type": "Product", "name": "{name}"}}</script></head>
<body><main><div class="hero"><figure><img src="/x.jpg"><figcaption>{name}</figcaption></figure></div>
<aside class="buy"><dl><dt>Price</dt><dd>{price}</dd></dl><button>Add to cart</button></aside>
<table class="specs"><tr><th>Size</th><td>M</td></tr></table><ol class="reviews"><li><blockquote>Nice</blockquote></li></ol></main></body></html>"""


def run(urls, observations=()):
    return infer([(u, "inventory") for u in urls], Config(), seed=BASE + "/", observations=list(observations))


def shape(pattern):
    return re.sub(r"\{[^}]+\}", "{}", pattern)


def test_nested_categories_and_pagination_stay_separate_and_unnamed():
    urls = [f"{BASE}/category/{c}" for c in ("men", "women", "kids")]
    urls += [f"{BASE}/category/men/{c}" for c in ("shirts", "jeans", "shoes")]
    urls += [f"{BASE}/category/men/page/{n}" for n in (2, 3, 4)]
    routes = {r.pattern: r for r in run(urls).routes}
    assert {"/category/{segment_2}", "/category/men/{segment_3}", "/category/men/page/{segment_4}"} <= set(routes)
    # No pagination evidence (no rel=next seen), so the number keeps a neutral name.
    assert routes["/category/men/page/{segment_4}"].parameters[0].type == "integer"


def test_same_role_with_materially_different_templates_stays_two_routes():
    urls = [f"{BASE}/item/i{n}" for n in range(10)]
    obs = [seen(u, product(n)) for n, u in enumerate(urls[:3])]
    obs += [seen(u, OTHER_PRODUCT.format(name=f"n{n}", price="1")) for n, u in enumerate(urls[3:6])]
    family = [r for r in run(urls, obs).routes if shape(r.pattern) == "/item/{}"]
    assert sorted((r.template, r.role) for r in family) == [("a", "product_detail"), ("b", "product_detail")]


def test_redirect_aliases_homepage_redirects_and_soft_404s_in_a_crawl(tmp_path):
    def routes(base):
        page = "text/html"
        products = [f"/product/item-{n}" for n in range(6)]
        pages = {"/robots.txt": ("text/plain", "Sitemap: /sitemap.xml\n"),
                 "/sitemap.xml": ("application/xml", urlset(base, products + ["/old-about"])),
                 "/": (page, html("Shop", ["/about", "/old-about"])), "/about": (page, html("About", ["/"])),
                 "/old-about": redirect("/about")}
        pages.update({p: (page, product(n)) for n, p in enumerate(products)})
        pages["/product/item-4"] = redirect("/")  # a sold-out product sent to the home page
        pages["/product/item-5"] = (page, html("Page not found", []))  # a 200 that is an error page
        return pages

    data = Site(routes).map(tmp_path, "--samples-per-family", "6", "--max-html-pages", "30")
    urls = {u["crawl_url"].split("/", 3)[3]: u for u in data["urls"]}
    assert urls["old-about"]["redirect_to"].endswith("/about")
    family = next(r for r in data["routes"] if shape(r["pattern"]) == "/product/{}")
    assert family["status"] == "validated_on_sample" and family["discovered_url_count"] == 6  # not collapsed
    unusable = {e["value"].split(":")[0] for e in family["evidence"] if e["kind"] == "unusable_sample"}
    assert unusable == {"redirected", "unsuitable"}


def test_budget_exhausted_by_many_small_families_keeps_them_provisional(tmp_path):
    def routes(base):
        paths = [f"/s{f}/p{n}" for f in range(30) for n in range(3)]
        pages = {"/robots.txt": ("text/plain", "Sitemap: /sitemap.xml\n"),
                 "/sitemap.xml": ("application/xml", urlset(base, paths)), "/": ("text/html", html("Home", []))}
        pages.update({p: ("text/html", ARTICLE.format(name=p)) for p in paths})
        return pages

    data = Site(routes).map(tmp_path, "--max-html-pages", "10")
    families = [r for r in data["routes"] if r["status"] != "individual"]
    assert len(families) == 30 and sum(r["status"] == "provisional" for r in families) >= 25
    assert any("validation" in reason for reason in data["crawl_report"]["stop_reasons"])
    assert sum(r["discovered_url_count"] for r in data["routes"]) == len(data["urls"])


def test_same_map_whatever_order_the_responses_come_back_in(tmp_path):
    def routes(base):
        page = "text/html"
        products = [f"/product/item-{n}" for n in range(40)]
        posts = [f"/blog/post-{n}" for n in range(8)]
        pages = {"/": (page, html("Shop", products[:12] + posts[:4] + ["/about", "/contact", "/product/compare"])),
                 "/about": (page, html("About", ["/"])), "/contact": (page, html("Contact", ["/"])),
                 "/product/compare": (page, COMPARE)}
        pages.update({p: (page, product(n).replace("</main>", "".join(f'<a href="{q}">{q}</a>' for q in products[n + 1:n + 4]) + "</main>"))
                      for n, p in enumerate(products)})
        # Each post links its own tag page: a new path shape reached from several pages of one wave, so
        # which tag gets fetched would follow the arrival order if waves were not sorted.
        pages.update({p: (page, ARTICLE.format(name=p).replace("</main>", f'<a href="/tag/t{n}">t{n}</a>' + "".join(
            f'<a href="{q}">{q}</a>' for q in posts) + "</main>")) for n, p in enumerate(posts)})
        pages.update({f"/tag/t{n}": (page, html(f"Tag {n}", [])) for n in range(len(posts))})
        return pages

    maps, site = [], Site(routes, jitter=0.05)
    for seed in (1, 2):  # the same site (same host and port), answering in a different order each time
        site.rng.seed(seed)
        data = site.map(tmp_path, "--max-html-pages", "20")
        maps.append({k: data[k] for k in ("routes", "urls", "tree", "links")} | {"observed": sorted(o["url_id"] for o in data["observations"])})
    assert maps[0] == maps[1]


def test_a_wave_picks_the_same_pages_whatever_order_they_were_queued_in():
    import random

    from route_map.spider import DiscoverySpider

    waiting = [(f"{BASE}/tag/t{n}", 2) for n in range(6)] + [(f"{BASE}/product/item-{n}", 1) for n in range(6)]
    picks = set()
    for seed in range(5):
        spider = DiscoverySpider(seed=BASE + "/", config=Config(), page_budget=50)
        spider.frontier = random.Random(seed).sample(waiting, len(waiting))
        picks.add(tuple(r.url for r in spider.wave()))
    assert picks == {(f"{BASE}/product/item-0", f"{BASE}/tag/t0")}  # one per new shape, shallow and sorted first

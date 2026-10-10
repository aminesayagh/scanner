import json
import re

from route_map.features import observe
from route_map.inference import infer
from route_map.models import Config, PageObservation
from route_map.normalization import normalize, url_id

from route_site import Site, html

PRODUCT = """<html><head><title>{name}</title><script type="application/ld+json">
{{"@context": "https://schema.org", "@type": "Product", "name": "{name}", "offers": {{"@type": "Offer", "price": "{price}"}}}}
</script></head><body><header><nav><a href="/">Home</a></nav></header><main><h1>{name}</h1>
<div class="gallery"><img src="/a.jpg"><img src="/b.jpg"></div><p class="price">{price}</p>
<form action="/cart/add" method="post"><select name="size"><option>S</option></select><button>Add to cart</button></form>
<section class="details"><h2>Details</h2><ul><li>Cotton</li><li>Made in Morocco</li></ul></section></main></body></html>"""

COMPARE = """<html><head><title>Compare</title></head><body><main><h1>Compare products</h1>
<table><thead><tr><th>Name</th><th>Price</th></tr></thead><tbody><tr><td>A</td><td>1</td></tr></tbody></table></main></body></html>"""

ARTICLE = """<html><head><title>{name}</title><script type="application/ld+json">{{"@graph": [{{"@type": "BlogPosting", "headline": "{name}"}}]}}</script></head>
<body><main><article><h1>{name}</h1><time datetime="2026-01-01">Jan 1</time><p>Text</p><p>More</p></article></main></body></html>"""


def uid(url):
    return url_id(normalize(url)["comparison_key"])


def seen(url, page, **extra):
    return PageObservation(**{"url_id": uid(url), "fetch_result": "ok", "status": 200, "final_url": url, **observe(page, url), **extra})


def product(n):
    return PRODUCT.format(name=f"Item {n} with a much longer name" * (n % 3 + 1), price=f"{n}.00")


def run(urls, observations, **config):
    return infer([(u, "inventory") for u in urls], Config(**config), seed="https://shop.test/", observations=observations)


def shape(pattern):
    return re.sub(r"\{[^}]+\}", "{}", pattern)  # parameter names follow roles; tested in test_route_roles


def by_pattern(result):
    return {(shape(r.pattern), r.template): r for r in result.routes}


def test_product_template_ignores_names_prices_and_reads_its_signals():
    a, b = observe(product(1), "https://shop.test/product/a"), observe(product(2), "https://shop.test/product/b")
    assert a["features"] == b["features"]
    assert a["structured_data_types"] == ["Product"] and "purchase" in a["signals"]
    assert observe(ARTICLE.format(name="x"), "https://shop.test/blog/x")["structured_data_types"] == ["BlogPosting"]


def test_family_is_validated_and_a_different_page_becomes_an_exception():
    urls = [f"https://shop.test/product/item-{n}" for n in range(10)] + ["https://shop.test/product/compare"]
    obs = [seen(u, product(n)) for n, u in enumerate(urls[:4])] + [seen(urls[-1], COMPARE)]
    result = run(urls, obs)
    family = by_pattern(result)[("/product/{}", None)]
    assert family.status == "validated_on_sample" and family.successful_sample_count == 4
    assert family.discovered_url_count == 10 and family.inferred_unsampled_url_count == 6
    assert family.excluded_literals == ["/product/compare"]
    compare = by_pattern(result)[("/product/compare", None)]
    assert compare.status == "individual" and family.exception_route_ids == [compare.id]
    kinds = {(e.kind, e.value) for e in family.evidence}
    assert ("structured_data", "Product") in kinds and ("signal", "purchase") in kinds


def test_same_pattern_with_two_templates_leaves_unsampled_members_ambiguous():
    urls = [f"https://shop.test/p/x{n}" for n in range(10)]
    obs = [seen(u, product(n)) for n, u in enumerate(urls[:3])] + [seen(u, ARTICLE.format(name=u)) for u in urls[3:6]]
    result = run(urls, obs)
    a, b = by_pattern(result)[("/p/{}", "a")], by_pattern(result)[("/p/{}", "b")]
    assert a.status == b.status == "validated_on_sample" and a.ambiguous_url_count == 4
    ambiguous = [u for u in result.urls if u.assignment == "ambiguous"]
    assert len(ambiguous) == 4 and all(u.route_id is None and u.candidate_route_ids == sorted([a.id, b.id]) for u in ambiguous)
    assert sum(r.discovered_url_count for r in result.routes) + len(ambiguous) == len(result.urls)  # counts reconcile


def test_same_layout_about_different_things_is_two_templates():
    urls = [f"https://shop.test/p/x{n}" for n in range(8)]
    article = PRODUCT.replace('"@type": "Product"', '"@type": "Article"')
    obs = [seen(u, product(n)) for n, u in enumerate(urls[:3])] + [seen(u, article.format(name="a", price="1")) for u in urls[3:6]]
    assert {(shape(r.pattern), r.template) for r in run(urls, obs).routes} == {("/p/{}", "a"), ("/p/{}", "b")}


def test_samples_that_do_not_agree_leave_the_family_unresolved():
    urls = [f"https://shop.test/x/v{n}" for n in range(6)]
    pages = [product(1), COMPARE, ARTICLE.format(name="a"), html("Plain", [])]
    result = run(urls, [seen(u, p) for u, p in zip(urls, pages)])
    family = by_pattern(result)[("/x/{}", None)]
    assert family.status == "unresolved" and family.discovered_url_count == 2
    assert {u.assignment for u in result.urls if u.route_id == family.id} == {"unresolved"}
    assert sum(r.status == "individual" for r in result.routes) == 4  # the observed pages, kept one by one


def test_redirected_and_soft_404_samples_do_not_collapse_the_family():
    urls = [f"https://shop.test/product/item-{n}" for n in range(8)]
    obs = [seen(u, product(n)) for n, u in enumerate(urls[:3])]
    obs.append(seen(urls[3], html("Home", []), final_url="https://shop.test/"))  # sent to the home page
    obs.append(PageObservation(url_id=uid(urls[4]), fetch_result="unsuitable", status=200, title="Page not found"))
    family = by_pattern(run(urls, obs))[("/product/{}", None)]
    assert family.status == "validated_on_sample" and family.discovered_url_count == 8
    assert {e.value.split(":")[0] for e in family.evidence if e.kind == "unusable_sample"} == {"redirected", "unsuitable"}


def test_too_few_usable_samples_stay_provisional():
    urls = [f"https://shop.test/product/item-{n}" for n in range(8)]
    family = by_pattern(run(urls, [seen(u, product(n)) for n, u in enumerate(urls[:2])]))[("/product/{}", None)]
    assert family.status == "provisional"


def test_crawl_validates_products_and_separates_compare(tmp_path):
    def routes(base):
        page = "text/html; charset=utf-8"
        products = [f"/product/item-{n}" for n in range(1000)]
        posts = [f"/blog/post-{n}" for n in range(6)]
        sitemap = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
            f"<url><loc>{base}{p}</loc></url>" for p in products + ["/product/compare"] + posts) + "</urlset>"
        pages = {"/robots.txt": ("text/plain", "Sitemap: /sitemap.xml\n"), "/sitemap.xml": ("application/xml", sitemap),
                 "/": (page, html("Shop", ["/product/item-1", "/blog/post-1"])), "/product/compare": (page, COMPARE)}
        pages.update({p: (page, product(int(p.rsplit("-", 1)[1]))) for p in products})
        pages.update({p: (page, ARTICLE.format(name=p)) for p in posts})
        return pages

    site = Site(routes)
    # 8 pages: 3 for discovery (home, a product, a post), 5 samples shared fairly between the two families.
    data = site.map(tmp_path, "--max-html-pages", "8")
    routes_ = {shape(r["pattern"]): r for r in data["routes"]}
    family = routes_["/product/{}"]
    assert family["status"] == "validated_on_sample" and family["successful_sample_count"] >= 3
    assert family["excluded_literals"] == ["/product/compare"] and routes_["/product/compare"]["status"] == "individual"
    assert routes_["/blog/{}"]["status"] == "validated_on_sample"  # the big family did not take every sample
    report = data["crawl_report"]
    assert report["discovery_html"] + report["validation_html"] <= 8

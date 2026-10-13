"""Template sampling after validation: families keep being sampled while their pages show templates not seen
yet, and stop once `saturation_window` pages in a row add none."""

from itertools import combinations

import pytest

from route_map.inference import families, records_from
from route_map.models import Config
from route_map.validation import jaccard, plan_templates

from route_site import Site, html, urlset
from test_route_validation import product, seen, uid

BASE = "https://shop.test"
PAGE = "text/html"


def section(name):
    """A page-builder section whose components only this name uses."""
    return (f'<section class="{name}"><div class="row"><x-{name}><div class="col">'
            f'<x-{name}-item><img src="/{name}.jpg"><span>{name}</span></x-{name}-item></div></x-{name}></div></section>')


def built(title, *names):
    return (f"<html><head><title>{title}</title></head><body><main><article><h1>{title}</h1>"
            + "".join(section(n) for n in names) + "</article></main></body></html>")


def layout(k, title):  # test 1's six layouts
    return built(title, f"l{k}-hero", f"l{k}-band")


def blog(posts, page_of):
    def routes(base):
        paths = [f"/blog/post-{n}" for n in range(posts)]
        pages = {"/robots.txt": ("text/plain", "Sitemap: /sitemap.xml\n"),
                 "/sitemap.xml": ("application/xml", urlset(base, paths)), "/": (PAGE, html("Home", []))}
        pages.update({p: (PAGE, page_of(n, p)) for n, p in enumerate(paths)})
        return pages
    return routes


def page_requests(site):
    return [p for p in site.requested if p not in ("/robots.txt", "/sitemap.xml")]


def posts_of(paths):
    return [p for p in paths if p.startswith("/blog/post-")]


@pytest.fixture(scope="module")
def six_layouts(tmp_path_factory):
    """Test 1's site mapped with and without template sampling: (map, page requests) each."""
    site, out = Site(blog(30, lambda n, p: layout(n % 6, p))), {}
    for flags in ((), ("--no-templates",)):  # template sampling is on by default
        site.requested.clear()
        out[flags] = site.map(tmp_path_factory.mktemp("blog"), *flags), page_requests(site)
    return out


def test_six_layouts_among_thirty_posts_are_all_found_before_every_post_is_fetched(six_layouts):
    data, requested = six_layouts[()]
    posts = {u["id"] for u in data["urls"] if "/blog/post-" in u["crawl_url"]}
    found = [t for t in data["templates"] if set(t["page_ids"]) <= posts]
    assert len(found) == 6
    assert not [r for r in data["crawl_report"]["stop_reasons"] if "/blog/" in r]
    assert len(posts_of(requested)) < 30


def test_without_template_sampling_only_the_validation_samples_are_fetched(six_layouts):
    config = Config()
    with_templates, without = six_layouts[()][1], six_layouts[("--no-templates",)][1]
    assert len(without) < len(with_templates)
    assert len(posts_of(without)) <= config.samples_per_family + config.extra_samples  # discovery fetched no post


def test_a_hundred_different_posts_use_the_budget_and_say_what_was_left(tmp_path):
    pages = [built(f"p{n}", f"p{n}-hero", f"p{n}-band") for n in range(3)]
    for a, b in combinations(pages, 2):  # each post is its own composition
        assert jaccard(*({*seen(f"{BASE}/x", p).features} for p in (a, b))) < 0.5
    site = Site(blog(100, lambda n, p: built(p, f"p{n}-hero", f"p{n}-band")))
    data = site.map(tmp_path, "--max-html-pages", "40")
    posts = {u["id"] for u in data["urls"] if "/blog/post-" in u["crawl_url"]}
    checked = {o["url_id"] for o in data["observations"]} & posts
    reasons = [r for r in data["crawl_report"]["stop_reasons"] if r.startswith("budget ran out before /blog/")]
    assert reasons == [f"budget ran out before /blog/{{segment_2}} was saturated: "
                       f"{100 - len(checked)} of 100 members not checked"]
    found = [t for t in data["templates"] if set(t["page_ids"]) & posts]
    assert len(found) == len(checked) and all(t["status"] == "single" for t in found)
    assert len(page_requests(site)) <= 40


def test_a_family_on_one_layout_gets_no_template_sampling(tmp_path):
    def routes(base):
        paths = [f"/product/item-{n}" for n in range(10)]
        pages = {"/robots.txt": ("text/plain", "Sitemap: /sitemap.xml\n"),
                 "/sitemap.xml": ("application/xml", urlset(base, paths)), "/": (PAGE, html("Home", []))}
        pages.update({p: (PAGE, product(n)) for n, p in enumerate(paths)})
        return pages

    site, fetched = Site(routes), {}
    for flag in ("--templates", "--no-templates"):
        site.requested.clear()
        site.map(tmp_path, flag)
        fetched[flag] = sorted(p for p in site.requested if p.startswith("/product/"))
    assert fetched["--templates"] == fetched["--no-templates"] and len(fetched["--templates"]) < 10


def test_a_family_saturates_once_its_pages_stop_adding_templates():
    config = Config(saturation_window=4)
    same = [f"{BASE}/same/item-{n}" for n in range(20)]
    new = [f"{BASE}/new/item-{n}" for n in range(20)]
    records, _ = records_from([(u, "inventory") for u in same + new], config, BASE + "/")
    candidates = families(records, config)
    pattern = {c.pattern.split("/")[1]: c.pattern for c in candidates}
    url_of = {uid(u): u for u in same + new}
    # Two checked pages each: too few to validate, so both families start open.
    observations = {o.url_id: o for o in [seen(u, layout(0, u)) for u in same[:2]] + [seen(u, built(u, f"n{n}")) for n, u in enumerate(new[:2])]}
    state, rounds = {}, 0
    while (picked := plan_templates(candidates, records, observations, config, state, budget=100)) and rounds < 10:
        rounds += 1
        for u in picked:
            n = int(url_of[u].rsplit("-", 1)[1])
            page = layout(0, url_of[u]) if "/same/" in url_of[u] else built(url_of[u], f"n{n}")
            observations[u] = seen(url_of[u], page)
        if rounds == 1:  # the first round checks `saturation_window` same-layout pages: none adds a template
            assert sum(u in observations for u in map(uid, same)) == 2 + config.saturation_window
            assert state[pattern["same"]][1] == 0
    assert state[pattern["same"]][1] >= config.saturation_window  # saturated after one quiet window
    assert sum(u in observations for u in map(uid, same)) == 2 + config.saturation_window
    assert all(u in observations for u in map(uid, new))  # each page added a template: checked to the end


def test_pages_that_are_not_html_use_one_budget_slot_each(tmp_path):
    """A non-HTML page leaves no observation; it is still requested once and counts against the page budget."""
    def routes(base):
        files, posts = [f"/files/f-{n}" for n in range(40)], [f"/blog/post-{n}" for n in range(10)]
        pages = {"/robots.txt": ("text/plain", "Sitemap: /sitemap.xml\n"),
                 "/sitemap.xml": ("application/xml", urlset(base, files + posts)), "/": (PAGE, html("Home", []))}
        pages.update({p: ("application/pdf", b"%PDF-1.4") for p in files})
        pages.update({p: (PAGE, layout(n % 6, p)) for n, p in enumerate(posts)})
        return pages

    site = Site(routes)
    site.map(tmp_path, "--max-html-pages", "20")
    requested = page_requests(site)
    assert len(requested) <= 20
    assert len(requested) == len(set(requested))  # no URL requested twice

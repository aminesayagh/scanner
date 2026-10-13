"""Template skeletons: the inside of a rich-text body counts as one token, the layout around it still counts.
Template inventory: every usable checked page of the site grouped by skeleton, whatever its URL."""

import random
from itertools import combinations

from parsel import Selector

from route_map.features import is_rich_text, main_root, observe, structure
from route_map.validation import jaccard

from route_map.export import summary
from route_map.inference import infer
from route_map.models import Config, PageObservation
from route_map.normalization import display_path

from test_route_roles import CONTACT
from test_route_validation import COMPARE, by_pattern, product, run, seen, uid

POST = """<html><head><title>{name}</title></head><body><header><nav><a href="/">Home</a><a href="/blog">Blog</a></nav></header>
<main><article><h1>{name}</h1><p class="meta">By Sara, <time datetime="2026-03-01">March 1</time></p>
<div class="post-body">{body}</div>
<aside class="related"><h2>Related</h2><ul><li><a href="/blog/a">A</a></li><li><a href="/blog/b">B</a></li></ul></aside>
</article></main><footer><p>Shop Ltd</p></footer></body></html>"""

INTRO = "<p>We tried three ways to ship faster.</p><p>Most of it was packing.</p>"
BODIES = {
    "plain": INTRO + "<p>Then we moved the warehouse.</p><blockquote><p>It paid off.</p></blockquote>",
    "code": INTRO + "<pre><code>ship --fast --region eu</code></pre><p>Run it nightly.</p><pre><code>ship --report</code></pre>",
    "table": INTRO + "<table><caption>Carriers</caption><thead><tr><th>Carrier</th><th>Days</th></tr></thead>"
                     "<tbody><tr><td><a href=\"/post\">Post</a></td><td>3</td></tr><tr><td><a href=\"/courier\">Courier</a></td>"
                     "<td>1</td></tr></tbody></table>",
    "video": INTRO + "<figure><iframe src=\"https://video.test/embed/1\"></iframe><figcaption>The new line</figcaption></figure>"
                     "<p>Watch the whole tour.</p>",
    "list": INTRO + "<ul>" + "".join(f"<li><strong>Step {n}</strong> <a href=\"/guide/{n}\">read</a></li>" for n in range(8))
                    + "</ul><h3>Next</h3><ol><li><strong>Repeat</strong></li></ol>",
}

BUILDER_A = """<html><head><title>Spring launch</title></head><body><main><article><h1>Spring launch</h1>
<section class="hero"><div class="row"><div class="col"><h2>New season</h2><p>Light fabrics for warm days.</p>
<a class="button" href="/shop">Shop now</a></div><div class="col"><img src="/hero.jpg"></div></div></section>
<section class="features"><div class="grid"><div class="card"><img src="/1.jpg"><h3>Linen</h3><p>Breathes.</p></div>
<div class="card"><img src="/2.jpg"><h3>Cotton</h3><p>Soft.</p></div><div class="card"><img src="/3.jpg"><h3>Wool</h3><p>Warm.</p></div>
</div></section></article></main></body></html>"""

BUILDER_B = """<html><head><title>Summer plans</title></head><body><main><article><h1>Summer plans</h1>
<section class="testimonials"><div class="slider"><div class="slide"><blockquote>Light fabrics for warm days.</blockquote>
<span class="author">Nadia</span></div><div class="slide"><blockquote>Soft and warm.</blockquote><span class="author">Omar</span></div></div></section>
<section class="pricing"><div class="plans"><div class="plan"><span class="name">Basic</span><span class="price">9</span>
<button>Choose</button></div><div class="plan"><span class="name">Plus</span><span class="price">19</span><button>Choose</button></div></div></section>
<section class="cta"><div class="band"><strong>Ready?</strong><a class="button" href="/shop">Shop now</a></div></section>
</article></main></body></html>"""


def skeleton(page, collapse=True):
    root, source = main_root(Selector(text=page))
    return structure(root, source == "heuristic", collapse_rich_text=collapse)


def posts():
    return {kind: POST.format(name=f"Shipping notes: {kind}", body=body) for kind, body in BODIES.items()}


def test_posts_with_one_layout_agree_whatever_their_body_holds():
    pages = posts()
    collapsed = {k: skeleton(p) for k, p in pages.items()}
    full = {k: skeleton(p, collapse=False) for k, p in pages.items()}
    assert all(jaccard(collapsed[a], collapsed[b]) >= 0.5 for a, b in combinations(pages, 2))
    assert any(jaccard(full[a], full[b]) < 0.5 for a, b in combinations(pages, 2))  # the collapse is what makes them agree
    assert {observe(p, "https://shop.test/blog/x")["rich_text_blocks"] for p in pages.values()} == {1}
    assert "article>div>rich_text" in collapsed["plain"] and "article>div>p" not in collapsed["plain"]


def test_page_builder_sections_stay_layout():
    a, b = skeleton(BUILDER_A), skeleton(BUILDER_B)
    assert jaccard(a, b) < 0.5
    assert {"article>section>div", "section>div>div"} <= a & b


def test_product_page_is_not_collapsed():
    page = product(1)
    root, _ = main_root(Selector(text=page))
    assert not is_rich_text(root) and not any(is_rich_text(el) for el in root.iter() if el.xpath(".//form"))
    assert observe(page, "https://shop.test/product/a")["rich_text_blocks"] == 0
    assert skeleton(page) == skeleton(page, collapse=False)


def test_a_card_is_layout_not_body_text():
    card = Selector(text='<div class="card"><img src="/a.jpg"><h3>Title</h3><p>Short text</p></div>').css("div")[0].root
    body = Selector(text="<div><h2>Title</h2><p>One</p><p>Two</p></div>").css("div")[0].root
    assert not is_rich_text(card) and is_rich_text(body)


BASE = "https://shop.test"
HOME = """<html><head><title>Amine</title></head><body><main><section class="hero"><h1>Amine</h1><p>I build shops.</p>
<a href="/contact/">Talk</a></section><section class="work"><div class="grid"><div class="card"><img src="/1.jpg"><h3>Shop</h3></div>
<div class="card"><img src="/2.jpg"><h3>Blog</h3></div></div></section></main></body></html>"""
BLOG_INDEX = """<html><head><title>Blog</title></head><body><main><h1>Blog</h1><ul class="posts">
<li><a href="/blog/a/"><h2>A</h2></a><p>Short</p></li><li><a href="/blog/b/"><h2>B</h2></a><p>Short</p></li></ul></main></body></html>"""
INFO = "<html><head><title>{name}</title></head><body><main><h1>T</h1><p>Text</p><p>More text</p></main></body></html>"


def template_of(result, url):
    tid = next(u.template_id for u in result.urls if u.crawl_url == url)
    return next((t for t in result.templates if t.id == tid), None)


def personal_site():
    """masayagh.com's shape: two posts with one layout, too few to make a URL family."""
    pages = posts()
    found = {"/": HOME, "/blog/": BLOG_INDEX, "/blog/shipping-notes/": pages["plain"], "/blog/code-review/": pages["code"],
             "/contact/": CONTACT}
    return [BASE + p for p in found], [seen(BASE + p, page) for p, page in found.items()]


def test_two_posts_outside_any_family_share_a_template():
    urls, obs = personal_site()
    result = run(urls, obs)
    assert {r.pattern: r.status for r in result.routes} == {p: "individual" for p in
                                                            ["/", "/blog/", "/blog/shipping-notes/", "/blog/code-review/", "/contact/"]}
    post = template_of(result, BASE + "/blog/shipping-notes/")
    assert post is template_of(result, BASE + "/blog/code-review/") and post.id == "template_001"
    assert post.status == "repeated" and post.checked_page_count == 2 and post.representative == "/blog/code-review/"
    assert {(e.kind, e.sample_support) for e in post.evidence} == {("skeleton", "2/2"), ("rich_text", "2/2")}
    assert len(result.templates) == 4 and all(t.status == "single" for t in result.templates[1:])
    text = summary(result)
    assert "4 templates." in text and f"| `/blog/code-review/` | repeated | 2 | 0 | {', '.join(post.route_ids)} |" in text


def test_one_template_spans_routes():
    urls = [BASE + "/about", BASE + "/privacy"]
    result = run(urls, [seen(u, INFO.format(name=u)) for u in urls])
    [info] = result.templates
    assert info.route_ids == sorted(r.id for r in result.routes) and len(info.route_ids) == 2
    assert info.page_ids == sorted(uid(u) for u in urls)


def test_a_family_exception_is_its_own_template_and_the_family_counts_its_unsampled_pages():
    urls = [f"{BASE}/product/item-{n}" for n in range(10)] + [BASE + "/product/compare"]
    result = run(urls, [seen(u, product(n)) for n, u in enumerate(urls[:5])] + [seen(urls[-1], COMPARE)])
    family = by_pattern(result)[("/product/{}", None)]
    products, compare = template_of(result, urls[0]), template_of(result, urls[-1])
    assert len(result.templates) == 2 and products is not compare
    assert products.checked_page_count == 5 and products.inferred_page_count == family.inferred_unsampled_url_count == 5
    assert compare.status == "single" and compare.inferred_page_count == 0
    assert ("structured_data", "Product") in {(e.kind, e.value) for e in products.evidence}
    assert all(u.template_id is None for u in result.urls if u.crawl_url in urls[5:10])  # unchecked


# alpha~beta 0.67, beta~gamma 0.71, alpha~gamma 0.43: beta is like both, but alpha and gamma are unlike each other.
CHAIN = {"/alpha": range(1, 5), "/docs/beta": range(1, 7), "/guides/x/gamma": range(2, 8)}


def test_templates_do_not_depend_on_host_or_input_order():
    """The page ids hold the host; the clustering order must not, nor the order pages and URLs come in. A page
    like two pages that are unlike each other does not bring them into one template."""
    def templates(base, shuffle):
        urls = [base + p for p in CHAIN]
        obs = [PageObservation(url_id=uid(base + p), fetch_result="ok", status=200, final_url=base + p,
                               features=[f"main>div>x{n}" for n in feats]) for p, feats in CHAIN.items()]
        random.Random(shuffle).shuffle(urls)
        random.Random(shuffle).shuffle(obs)
        result = infer([(u, "inventory") for u in urls], Config(), seed=base + "/", observations=obs)
        path = {u.id: display_path(u.crawl_url) for u in result.urls}
        return [(t.id, t.representative, t.status, sorted(path[u] for u in t.page_ids)) for t in result.templates]

    first = templates("https://shop.test", 0)
    assert not any({"/alpha", "/guides/x/gamma"} <= set(pages) for *_, pages in first)
    for base in ("https://shop.test", "http://127.0.0.1:5000"):
        for shuffle in range(3):
            assert templates(base, shuffle) == first


def test_unusable_pages_are_in_no_template():
    urls = [f"{BASE}/product/item-{n}" for n in range(8)]
    obs = [seen(u, product(n)) for n, u in enumerate(urls[:3])]
    obs.append(seen(urls[3], HOME, final_url=BASE + "/"))  # sent to the home page
    obs.append(PageObservation(url_id=uid(urls[4]), fetch_result="unsuitable", status=200, title="Page not found"))
    result = run(urls, obs)
    [products] = result.templates
    assert products.page_ids == sorted(uid(u) for u in urls[:3])
    assert [u.template_id for u in result.urls if u.crawl_url in urls[3:5]] == [None, None]
    # inferred: only the 3 members with no observation at all, not the redirected and the error page
    family = by_pattern(result)[("/product/{}", None)]
    observed = {o.url_id for o in result.observations}
    unplaced = [u for u in result.urls if u.route_id == family.id and u.id in observed and u.template_id is None]
    assert products.checked_page_count == 3 and products.inferred_page_count == 3 and len(unplaced) == 2
    assert products.checked_page_count + products.inferred_page_count + len(unplaced) == family.discovered_url_count == 8


CATEGORY = """<html><head><title>Shirts</title></head><body><main><div class="container"><h1>Shirts</h1><p>Light shirts.</p>
<p>Made in Fes.</p><ul class="products">""" + "".join(
    f'<li><a href="/product/shirt-{n}"><img src="/{n}.jpg"><h3>Shirt {n}</h3><span class="price">{n}9.00</span></a></li>'
    for n in range(6)) + """</ul><div class="pager"><a href="?page=2">Next</a></div></div></main></body></html>"""
ABOUT = """<html><head><title>About</title></head><body><main><div class="container"><h1>About us</h1><p>We sew.</p>
<p>Since 1990.</p><ul><li>Linen</li><li>Cotton</li><li>Wool</li></ul><p>Visit us.</p></div></main></body></html>"""


def test_a_card_list_inside_a_page_wrapper_stays_layout():
    """A wrapper holding a heading, two paragraphs and a product grid is a listing page, not a rich-text body."""
    urls = [BASE + "/category/shirts", BASE + "/about"]
    result = run(urls, [seen(urls[0], CATEGORY), seen(urls[1], ABOUT)])
    assert len(result.templates) == 2
    assert "div>rich_text" in skeleton(ABOUT) and "div>rich_text" not in skeleton(CATEGORY)

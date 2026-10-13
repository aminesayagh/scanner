"""Template skeletons: the inside of a rich-text body counts as one token, the layout around it still counts."""

from itertools import combinations

from parsel import Selector

from route_map.features import is_rich_text, main_root, observe, structure
from route_map.validation import jaccard

from test_route_validation import product

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

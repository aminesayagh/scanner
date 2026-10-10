import json

from typer.testing import CliRunner

from route_map.cli import app
from route_map.inference import infer
from route_map.models import Config

from test_route_validation import ARTICLE, COMPARE, PRODUCT, product, seen

LISTING = """<html><head><title>{name}</title></head><body><main><h1>{name}</h1><ul class="grid">{items}</ul></main></body></html>"""
CONTACT = """<html><head><title>Contact</title></head><body><main><h1>Contact us</h1>
<form action="/contact" method="post"><input type="email" name="email"><textarea name="message"></textarea><button>Send</button></form></main></body></html>"""
LOGIN = """<html><head><title>Sign in</title></head><body><main><form><input name="email"><input type="password" name="password">
<button>Sign in</button></form></main></body></html>"""
HEADER_LOGIN = """<html><head><title>Story</title></head><body><header><form><input type="password"></form></header>
<main><h1>Our story</h1><p>Text</p></main></body></html>"""
BASE = "https://shop.test"


def listing(name, links):
    return LISTING.format(name=name, items="".join(f'<li><a href="{h}">{h}</a></li>' for h in links))


def run(urls, observations):
    return infer([(u, "inventory") for u in urls], Config(), seed=BASE + "/", observations=observations)


def route(result, url):
    rid = next(u.route_id for u in result.urls if u.crawl_url == url)
    return next(r for r in result.routes if r.id == rid)


def shop():
    products = [f"{BASE}/product/item-{n}" for n in range(6)]
    collections = [f"{BASE}/collections/c{n}" for n in range(3)]
    posts = [f"{BASE}/blog/post-{n}" for n in range(3)]
    urls = products + collections + posts + [f"{BASE}/", f"{BASE}/contact", f"{BASE}/account/login", f"{BASE}/story"]
    obs = [seen(u, product(n)) for n, u in enumerate(products[:3])]
    obs += [seen(u, listing(u, products[:5]), links=products[:5]) for u in collections]
    obs += [seen(u, ARTICLE.format(name=u)) for u in posts]
    obs += [seen(f"{BASE}/", listing("Home", collections)), seen(f"{BASE}/contact", CONTACT),
            seen(f"{BASE}/account/login", LOGIN), seen(f"{BASE}/story", HEADER_LOGIN)]
    return run(urls, obs)


def test_roles_from_page_cues_and_parameter_names_after_them():
    result = shop()
    detail = route(result, f"{BASE}/product/item-0")
    assert (detail.role, detail.role_certainty, detail.pattern) == ("product_detail", "supported", "/product/{product_slug}")
    post = route(result, f"{BASE}/blog/post-0")
    assert (post.role, post.role_certainty, post.pattern) == ("article", "supported", "/blog/{article_slug}")
    collection = route(result, f"{BASE}/collections/c0")
    assert (collection.role, collection.role_certainty) == ("product_listing", "supported")
    assert route(result, f"{BASE}/").role == "home"
    assert route(result, f"{BASE}/contact").role == "contact"
    assert route(result, f"{BASE}/account/login").role == "authentication"


def test_a_sign_in_box_in_the_header_does_not_make_a_login_page():
    assert route(shop(), f"{BASE}/story").role != "authentication"


def test_numeric_product_ids_are_named_product_id():
    urls = [f"{BASE}/item/{n}" for n in range(100, 106)]
    result = run(urls, [seen(u, product(n)) for n, u in enumerate(urls[:3])])
    assert route(result, urls[0]).pattern == "/item/{product_id}"


def test_url_words_alone_give_a_weak_role_and_keep_neutral_names():
    result = run([f"{BASE}/products/p{n}" for n in range(5)], [])
    family = result.routes[0]
    assert (family.role, family.role_certainty, family.pattern) == ("product_detail", "weak", "/products/{segment_2}")
    assert any(e.kind == "role" and "URL words" in e.value for e in family.evidence)


def test_conflicting_cues_give_unknown_with_the_reason():
    both = PRODUCT.replace("</main>", '<form><input type="email"><textarea></textarea></form></main>')
    urls = [f"{BASE}/x/v{n}" for n in range(5)]
    family = run(urls, [seen(u, both.format(name="a", price="1")) for u in urls[:3]]).routes[0]
    assert family.role == "unknown" and any("conflicting" in e.value for e in family.evidence)


def test_two_templates_under_one_pattern_get_their_own_names():
    urls = [f"{BASE}/p/x{n}" for n in range(10)]
    obs = [seen(u, product(n)) for n, u in enumerate(urls[:3])] + [seen(u, ARTICLE.format(name=u)) for u in urls[3:6]]
    assert {r.pattern for r in run(urls, obs).routes} == {"/p/{product_slug}", "/p/{article_slug}"}


def test_unresolved_family_has_an_unknown_role():
    urls = [f"{BASE}/x/v{n}" for n in range(6)]
    pages = [product(1), COMPARE, ARTICLE.format(name="a"), LOGIN]
    result = run(urls, [seen(u, p) for u, p in zip(urls, pages)])
    unresolved = next(r for r in result.routes if r.status == "unresolved")
    assert unresolved.role == "unknown"


def test_prefix_tree_marks_grouping_nodes_synthetic():
    result = shop()
    nodes = {}

    def walk(node):
        nodes[node["path"]] = node
        for child in node["children"]:
            walk(child)

    walk(result.tree)
    assert nodes["/product"]["synthetic"] and not nodes["/product"]["route_ids"]  # only /product/{...} exists
    assert not nodes["/contact"]["synthetic"] and nodes["/"]["route_ids"]


def test_links_between_routes_are_the_observed_ones():
    result = shop()
    collection = route(result, f"{BASE}/collections/c0").id
    detail = route(result, f"{BASE}/product/item-0").id
    edge = next(e for e in result.links if e["from"] == collection and e["to"] == detail)
    assert edge["pages"] == 3 and edge["links"] == 15
    assert not any(e["from"] == detail for e in result.links)  # product pages link nowhere here


def test_cli_writes_a_markdown_summary(tmp_path):
    out, md = tmp_path / "map.json", tmp_path / "map.md"
    result = CliRunner().invoke(app, [BASE + "/", "--inventory", "fixtures/route-map/shop-inventory.txt", "-o", str(out),
                                      "--summary", str(md)])
    assert result.exit_code == 0, result.output
    text = md.read_text()
    assert text.startswith("# Route map of https://shop.test/") and "| `/product/{segment_2}` | provisional |" in text
    assert json.loads(out.read_text())["tree"]["children"]


def test_product_page_with_a_related_products_grid_stays_a_product_page():
    # As on Shopify: /collections/<c>/products/<slug> pages show a grid of /products/<slug> links.
    products = [f"{BASE}/products/item-{n}" for n in range(8)]
    in_collection = [f"{BASE}/collections/all/products/item-{n}" for n in range(8)]
    related = '<section class="related"><ul>' + "".join(f'<li><a href="{p}">{p}</a></li>' for p in products[3:8]) + "</ul></section>"
    page = PRODUCT.replace("</main>", related + "</main>")
    obs = [seen(u, product(n)) for n, u in enumerate(products[:3])]
    obs += [seen(u, page.format(name=f"n{n}", price="1"), links=products[3:8]) for n, u in enumerate(in_collection[:3])]
    detail = route(run(products + in_collection, obs), in_collection[0])
    assert (detail.role, detail.role_certainty) == ("product_detail", "supported")


def test_a_search_page_listing_products_is_search_results():
    base, products = shop(), [f"{BASE}/product/item-{n}" for n in range(5)]
    search = seen(f"{BASE}/search", listing("Search", products), links=products)
    result = run([u.crawl_url for u in base.urls] + [f"{BASE}/search"], base.observations + [search])
    assert route(result, f"{BASE}/search").role == "search_results"


def test_a_cue_on_a_minority_of_samples_does_not_beat_url_words():
    urls = [f"{BASE}/pages/{n}" for n in ("contact", "story", "care", "faq-2", "team")]
    # One shared layout; the contact page only adds a small form, so it stays in the family.
    plain = ("<html><head><title>{t}</title></head><body><main><h1>{t}</h1><section><h2>A</h2><ul><li><a href='/'>x</a></li>"
             "</ul><p><em>Text</em></p></section><aside><h3>B</h3><p><strong>More</strong></p></aside>{extra}</main></body></html>")
    form = '<form><input type="email"><textarea></textarea></form>'
    pages = [plain.format(t="Contact", extra=form)] + [plain.format(t=n, extra="") for n in range(4)]
    result = run(urls, [seen(u, p) for u, p in zip(urls, pages)])
    family = next(r for r in result.routes if r.pattern.startswith("/pages/{"))
    assert family.discovered_url_count == 5  # the contact page is a member, its form a minority cue
    assert (family.role, family.role_certainty) == ("informational", "weak")


def test_an_exception_does_not_take_its_role_from_the_family_url_words():
    products = [f"{BASE}/product/item-{n}" for n in range(6)] + [f"{BASE}/product/compare"]
    result = run(products, [seen(u, product(n)) for n, u in enumerate(products[:3])] + [seen(products[-1], COMPARE)])
    assert route(result, products[-1]).role == "unknown"

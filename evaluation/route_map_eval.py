"""route-map evaluation on a local shop whose families and roles are known.

    uv run python evaluation/route_map_eval.py            # prints a Markdown table

Measures, per page budget: pairwise grouping precision/recall, false merges and splits, singleton
preservation, role accuracy on supported roles (weak/unknown counted apart), discovery coverage, page
requests per validated family, and whether two crawls answered in a different order give the same map.
Templates: found against true, false merges and splits over the checked pages, page requests per template found.
"""

import json
import random
import sys
import tempfile
from collections import Counter
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from route_site import Site, html, urlset  # noqa: E402
from test_route_roles import CONTACT, LOGIN, listing  # noqa: E402
from test_route_validation import ARTICLE, COMPARE, product  # noqa: E402

CART = """<html><head><title>Cart</title></head><body><main><h1>Your cart</h1><table><tr><td>Item</td></tr></table>
<a href="/checkout">Check out</a></main></body></html>"""
INFO = "<html><head><title>{t}</title></head><body><main><h1>{t}</h1><p>Text</p><p>More text</p></main></body></html>"
ADJECTIVES = ["red", "blue", "linen", "denim", "oak", "wool", "silk", "black", "green", "soft"]
NOUNS = ["chair", "shirt", "jacket", "table", "lamp", "skirt", "scarf", "boot", "bag", "vest"]


def truth():
    """{path: (family, role)} for every page of the shop; families of one page are singletons."""
    rng = random.Random(7)
    products = sorted({f"/product/{rng.choice(ADJECTIVES)}-{rng.choice(NOUNS)}-{n}" for n in range(300)})
    out = {p: ("product", "product_detail") for p in products}
    out |= {f"/collections/{c}": ("collection", "product_listing") for c in ("men", "women", "kids", "sale", "new", "linen", "denim", "home")}
    out |= {f"/blog/{a}-{n}": ("post", "article") for n, a in enumerate(ADJECTIVES * 2)}
    out |= {f"/blog/category/{c}": ("blog_category", "article_listing") for c in ("news", "guides", "stories", "care")}
    out |= {f"/search?q={q}": ("search", "search_results") for q in ("shirt", "lamp", "denim")}
    out |= {"/": ("/", "home"), "/about": ("/about", "informational"), "/privacy": ("/privacy", "informational"),
            "/contact": ("/contact", "contact"), "/cart": ("/cart", "cart"), "/account/login": ("/account/login", "authentication"),
            "/product/compare": ("/product/compare", None)}
    return out


# Families whose pages render one shared layout (see site_routes): about and privacy use INFO; collections, blog
# categories and search pages all use the same `listing` page. Every other family is its own template.
SHARED_TEMPLATES = {"/about": "info", "/privacy": "info", "collection": "listing", "blog_category": "listing",
                    "search": "listing"}


def true_template(family):
    return SHARED_TEMPLATES.get(family, family)


def site_routes(base):
    t = truth()
    products = [p for p, (f, _) in t.items() if f == "product"]
    posts = [p for p, (f, _) in t.items() if f == "post"]
    collections = [p for p, (f, _) in t.items() if f == "collection"]
    categories = [p for p, (f, _) in t.items() if f == "blog_category"]
    searches = [p for p, (f, _) in t.items() if f == "search"]
    page = "text/html; charset=utf-8"
    pages = {"/robots.txt": ("text/plain", "Sitemap: /sitemap.xml\n"),
             "/sitemap.xml": ("application/xml", urlset(base, [p for p in t if not p.startswith("/search")])),
             "/": (page, html("Shop", collections + ["/blog/category/news", "/about", "/contact", "/privacy", "/cart",
                                                      "/account/login", "/search?q=shirt", "/product/compare"]))}
    for n, p in enumerate(products):
        pages[p] = (page, product(n))
    for n, c in enumerate(collections):
        pages[c] = (page, listing(c, products[n * 6:n * 6 + 6]))
    for n, p in enumerate(posts):
        pages[p] = (page, ARTICLE.format(name=p))
    for n, c in enumerate(categories):
        pages[c] = (page, listing(c, posts[n * 5:n * 5 + 5]))
    for n, s in enumerate(searches):
        pages[s] = (page, listing("Search", products[n * 4:n * 4 + 5]))
    pages |= {"/about": (page, INFO.format(t="About")), "/privacy": (page, INFO.format(t="Privacy")),
              "/contact": (page, CONTACT), "/cart": (page, CART), "/account/login": (page, LOGIN),
              "/product/compare": (page, COMPARE)}
    return pages


def path_of(url):
    rest = url.split("/", 3)[3] if url.count("/") >= 3 else ""
    return "/" + rest


def measure(data, t):
    urls = [u for u in data["urls"] if path_of(u["crawl_url"]) in t]
    pred = [u["route_id"] or u["id"] for u in urls]  # an ambiguous URL counts as its own group
    true = [t[path_of(u["crawl_url"])][0] for u in urls]
    cells = Counter(zip(pred, true))
    tp = sum(comb(n, 2) for n in cells.values())
    pred_pairs = sum(comb(n, 2) for n in Counter(pred).values())
    true_pairs = sum(comb(n, 2) for n in Counter(true).values())
    by_pred, by_true = {}, {}
    for p, f in zip(pred, true):
        by_pred.setdefault(p, set()).add(f)
        by_true.setdefault(f, set()).add(p)
    singletons = [f for f in by_true if sum(1 for x in true if x == f) == 1]
    routes = {r["id"]: r for r in data["routes"]}
    kept_alone = sum(1 for f in singletons if all(routes.get(p, {}).get("status") == "individual" for p in by_true[f]))
    roles_ok = roles_all = weak = unknown = 0
    for rid, families in by_pred.items():
        r = routes.get(rid)
        if not r:
            continue
        expected = Counter(t[path_of(u["crawl_url"])][1] for u in urls if u["route_id"] == rid).most_common(1)[0][0]
        if r["role_certainty"] == "supported" and expected:
            roles_all += 1
            roles_ok += r["role"] == expected
        weak += r["role_certainty"] == "weak"
        unknown += r["role_certainty"] == "unknown"
    report = data["crawl_report"]
    validated = sum(r["status"] == "validated_on_sample" for r in data["routes"])
    return {
        "precision": tp / pred_pairs if pred_pairs else 1.0,
        "recall": tp / true_pairs if true_pairs else 1.0,
        "false_merges": sum(len(f) > 1 for f in by_pred.values()),
        "false_splits": sum(len(p) > 1 for f, p in by_true.items() if f not in singletons),
        "singletons": f"{kept_alone}/{len(singletons)}",
        "roles": f"{roles_ok}/{roles_all}",
        "weak_unknown": f"{weak}/{unknown}",
        "coverage": len(urls) / len(t),
        "pages": report["discovery_html"] + report["validation_html"],
        "per_family": (report["discovery_html"] + report["validation_html"]) / validated if validated else None,
        "validated": validated,
        **template_measures(data, urls, t),
    }


def template_measures(data, urls, t):
    """Over the checked pages (those with a template): a false merge is a found template holding pages of two true
    templates; a false split is a true template whose checked pages sit in two found templates."""
    checked = [(u["template_id"], true_template(t[path_of(u["crawl_url"])][0])) for u in urls if u["template_id"]]
    by_found, by_true = {}, {}
    for found, true in checked:
        by_found.setdefault(found, set()).add(true)
        by_true.setdefault(true, set()).add(found)
    pages = data["crawl_report"]["discovery_html"] + data["crawl_report"]["validation_html"]
    return {
        "templates": f"{len(by_found)}/{len({true_template(f) for f, _ in t.values()})}",
        "true_reached": len(by_true),
        "template_merges": sum(len(x) > 1 for x in by_found.values()),
        "template_splits": sum(len(x) > 1 for x in by_true.values()),
        "per_template": pages / len(by_found) if by_found else None,
    }


def main():
    t = truth()
    site = Site(site_routes, jitter=0.02)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for budget in (20, 50, 100):
            maps = []
            for seed in (1, 2):  # same site, answers in a different order
                site.rng.seed(seed)
                out = Path(tmp) / f"{budget}-{seed}"
                out.mkdir()
                maps.append(site.map(out, "--max-html-pages", str(budget)))
            same = all(json.dumps(maps[0][k], sort_keys=True) == json.dumps(maps[1][k], sort_keys=True)
                       for k in ("routes", "urls", "templates", "tree", "links"))
            rows.append((budget, measure(maps[0], t), same))
    print("| Budget | Pages used | Validated families | Pages per validated family | Precision | Recall | False merges "
          "| False splits | Singletons kept | Supported roles right | Weak / unknown routes | Coverage | Templates found / true "
          "| True templates checked | Template false merges | Template false splits | Pages per template | Same map twice |")
    print("|" + "---|" * 18)
    for budget, m, same in rows:
        per = f"{m['per_family']:.1f}" if m["per_family"] else "—"
        per_template = f"{m['per_template']:.1f}" if m["per_template"] else "—"
        print(f"| {budget} | {m['pages']} | {m['validated']} | {per} | {m['precision']:.3f} | {m['recall']:.3f} | "
              f"{m['false_merges']} | {m['false_splits']} | {m['singletons']} | {m['roles']} | {m['weak_unknown']} | "
              f"{m['coverage']:.0%} | {m['templates']} | {m['true_reached']} | {m['template_merges']} | {m['template_splits']} | "
              f"{per_template} | {'yes' if same else 'NO'} |")


if __name__ == "__main__":
    main()

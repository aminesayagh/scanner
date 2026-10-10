"""Page observations from HTML: the main content's structure, structured data, breadcrumbs and functional
signals. Used to check that the sampled pages of a family share one template.

Structure is a set of tag paths (an element with up to two ancestors inside the main content, e.g.
"ul>li>a"): text, prices, generated ids and how many cards repeat do not change it, while a different
component does. Scripts, styles and SVG are ignored; without a <main>, [role=main] or <article>, the body
minus header, nav, footer and aside is used and the evidence is marked weaker ("heuristic").
"""

import json
import re
from urllib.parse import urljoin

from parsel import Selector

from .discovery import path_shape

SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "link", "meta"}
CHROME = {"header", "nav", "footer", "aside"}
# Structured-data types that name what a page is about; two samples with different ones are different pages.
ROLE_TYPES = {"Product", "ProductGroup", "Article", "BlogPosting", "NewsArticle", "Recipe", "Event", "JobPosting",
              "CollectionPage", "SearchResultsPage", "ContactPage", "AboutPage", "FAQPage", "ItemList", "Offer"}
_PURCHASE = re.compile(r"add[\s_-]*to[\s_-]*(cart|bag|basket)|buy[\s_-]*now|ajouter au panier|in den warenkorb", re.I)


def _types(node, out: list, top: bool):
    if isinstance(node, list):
        for n in node:
            _types(n, out, top)
    elif isinstance(node, dict):
        t = node.get("@type")
        if top and t:
            out.extend(t if isinstance(t, list) else [t])
        if "@graph" in node:
            _types(node["@graph"], out, True)  # @graph members are top-level entities


def structured_data(sel: Selector) -> tuple[list[str], list[str]]:
    """(primary entity types, breadcrumb names) from JSON-LD, arrays and @graph included. Nested entities
    (an Offer inside a Product) are not primary. Broken JSON is skipped: the markup may be incomplete."""
    types, crumbs = [], []
    for raw in sel.css('script[type="application/ld+json"]::text').getall():
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        _types(data, types, True)
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if node.get("@type") == "BreadcrumbList":
                    items = sorted(node.get("itemListElement") or [], key=lambda i: i.get("position", 0) if isinstance(i, dict) else 0)
                    crumbs = [str(i.get("name") or (i.get("item") or {}).get("name", "")) for i in items if isinstance(i, dict)]
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return sorted({str(t) for t in types}), crumbs


def main_root(sel: Selector):
    """(element, "main" | "heuristic")."""
    for query in ("main", "[role=main]", "article"):
        if found := sel.css(query):
            return found[0].root, "main"
    body = sel.css("body")
    return (body[0].root if body else sel.root), "heuristic"


def structure(root, skip_chrome: bool) -> set[str]:
    out = set()

    def walk(el, trail):
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else None
            if tag is None or tag in SKIP or (skip_chrome and tag in CHROME):
                continue
            path = (trail + (tag,))[-3:]
            out.add(">".join(path))
            walk(child, path)

    walk(root, ())
    return out


def signals(sel: Selector, main, url: str) -> list[str]:
    """Functional cues: purchase controls, a product/listing grid, article metadata, search, sign-in, contact."""
    found = set()
    m = Selector(root=main)
    buttons = " ".join(m.css("button ::text, input[type=submit]::attr(value), button::attr(name), input::attr(name)").getall())
    if _PURCHASE.search(buttons) or m.css('form[action*="cart"]') or m.css('[itemprop="price"], [itemprop="offers"]'):
        found.add("purchase")
    shapes = {}
    for href in m.css("a::attr(href)").getall():
        shape = path_shape(urljoin(url, href))
        shapes[shape] = shapes.get(shape, 0) + 1
    if any(n >= 4 and shape[0] >= 2 for shape, n in shapes.items()):
        found.add("listing")  # four or more links to one path family inside the main content
    if m.css("article time[datetime], time[datetime][itemprop*=date]") or sel.css(
            'meta[property="article:published_time"], meta[property="og:type"][content="article"]'):
        found.add("article_meta")
    if sel.css('input[type=search], form[role=search], input[name=q], input[name=s]'):
        found.add("search")
    if sel.css("input[type=password]"):
        found.add("password")
    if m.css("form textarea") and m.css('form input[type=email], form input[name*=mail]'):
        found.add("contact_form")
    return sorted(found)


def observe(html: str, url: str) -> dict:
    """PageObservation fields (without url_id, fetch result and status) of one HTML page."""
    sel = Selector(text=html)
    main, source = main_root(sel)
    types, crumbs = structured_data(sel)
    if not crumbs:
        crumbs = [t.strip() for t in sel.css('nav[aria-label*="readcrumb"] a::text, .breadcrumb a::text').getall() if t.strip()]
    found = signals(sel, main, url)
    canonical = sel.css('link[rel~="canonical"]::attr(href)').get()
    return {
        "title": (sel.css("title::text").get() or "").strip() or None,
        "features": sorted(structure(main, source == "heuristic") | {f"signal:{s}" for s in found}),
        "structured_data_types": types,
        "breadcrumbs": crumbs,
        "signals": found,
        "main_source": source,
        "canonical": urljoin(url, canonical) if canonical else None,
    }

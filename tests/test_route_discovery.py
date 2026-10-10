import gzip
import http.server
import json
import subprocess
import sys
import threading

import pytest

from route_map.discovery import SitemapError, is_action, is_asset, read_sitemap, robots_sitemaps

URLSET = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</urlset>'
INDEX = '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</sitemapindex>'


def urlset(base, paths):
    return URLSET.format("".join(f"<url><loc>{base}{p}</loc></url>" for p in paths))


def index(base, paths):
    return INDEX.format("".join(f"<sitemap><loc>{base}{p}</loc></sitemap>" for p in paths))


def html(title, links):
    return f"<html><head><title>{title}</title></head><body>" + "".join(f'<a href="{h}">{h}</a>' for h in links) + "</body></html>"


class Site:
    """A local site: `routes(base)` returns {path: (content type, body)}; every requested path is logged."""

    def __init__(self, routes):
        self.requested = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                site.requested.append(self.path)
                found = site.pages.get(self.path)
                if found is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                kind, body = found
                body = body.encode() if isinstance(body, str) else body
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.pages = routes(self.base)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def map(self, tmp_path, *args):
        out = tmp_path / "map.json"
        # One crawl per process: Scrapy's event loop cannot be restarted inside one.
        done = subprocess.run([sys.executable, "-m", "route_map", self.base + "/", "-o", str(out), *args],
                              capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        return json.loads(out.read_text())


@pytest.fixture
def shop():
    def routes(base):
        products = [f"/product/item-{n}" for n in range(1000)]
        page = "text/html; charset=utf-8"
        pages = {
            "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private\nSitemap: /sitemap_index.xml\n"),
            # The index lists itself (a cycle) and a malformed file next to the real ones.
            "/sitemap_index.xml": ("application/xml", index(base, ["/sitemap-products.xml.gz", "/sitemap-pages.xml",
                                                                   "/sitemap_index.xml", "/broken.xml"])),
            "/sitemap-products.xml.gz": ("application/gzip", gzip.compress(urlset(base, products).encode())),
            "/sitemap-pages.xml": ("application/xml", urlset(base, ["/", "/about", "/contact"])),
            "/broken.xml": ("application/xml", "<urlset><url><loc>oops"),
            "/": (page, html("Shop", ["/about", "/contact", "/product/item-1", "/product/item-2", "/blog/first",
                                      "/cart/add?id=1", "/logout", "/logo.png", "/private/area",
                                      "https://elsewhere.test/page", "mailto:hi@shop.test"])),
            "/about": (page, html("About", ["/"])),
            "/contact": (page, html("Contact", ["/"])),
            "/blog/first": (page, html("First post", ["/blog/second", "/blog/third"])),
            "/blog/second": (page, html("Second post", [])),
            "/blog/third": (page, html("Third post", [])),
            "/logo.png": ("image/png", b"\x89PNG"),
            "/private/area": (page, html("Private", [])),
        }
        for p in products:
            n = int(p.rsplit("-", 1)[1])
            pages[p] = (page, html(f"Item {n}", [f"/product/item-{(n + k) % 1000}" for k in range(1, 6)]))
        return pages

    return Site(routes)


def test_sitemap_products_are_discovered_without_fetching_each_page(shop, tmp_path):
    data = shop.map(tmp_path, "--max-html-pages", "20")
    product = next(r for r in data["routes"] if r["pattern"] == "/product/{segment_2}")
    assert product["discovered_url_count"] == 1000
    report = data["crawl_report"]
    assert report["discovery_html"] <= 10  # half of the page budget; the rest is kept for validation
    fetched_products = [p for p in shop.requested if p.startswith("/product/")]
    assert len(fetched_products) <= 10
    assert report["requests_used"] < 25


def test_sitemap_cycles_and_malformed_files_are_handled(shop, tmp_path):
    report = shop.map(tmp_path, "--max-html-pages", "6")["crawl_report"]
    assert shop.requested.count("/sitemap_index.xml") == 1  # the self-reference is not followed again
    errors = {r["url"].rsplit("/", 1)[1]: r.get("error") for r in report["sitemap_results"]}
    assert errors["broken.xml"].startswith("malformed XML") and errors["sitemap-pages.xml"] is None


def test_action_links_assets_other_hosts_and_robots_rules_are_respected(shop, tmp_path):
    report = shop.map(tmp_path, "--max-html-pages", "40")["crawl_report"]
    assert not any(p.startswith(("/cart/add", "/logout", "/logo.png", "/private")) for p in shop.requested)
    assert report["skipped_hosts"] == ["elsewhere.test"]
    assert any(u.endswith("/private/area") for u in report["blocked_urls"])


def test_novel_path_shapes_are_fetched_before_more_products(tmp_path):
    def routes(base):
        products = [f"/product/p{n}" for n in range(30)]
        return {"/": ("text/html", html("Home", products + ["/blog/first"])),  # the blog link comes last
                **{p: ("text/html", html(p, [])) for p in products}, "/blog/first": ("text/html", html("Post", []))}

    site = Site(routes)
    site.map(tmp_path, "--max-html-pages", "6")  # 3 discovery pages: the home page and two more
    assert "/blog/first" in site.requested
    assert sum(p.startswith("/product/") for p in site.requested) == 1  # one member shows the family


def test_page_budget_is_reported_as_a_stop_reason(shop, tmp_path):
    report = shop.map(tmp_path, "--max-html-pages", "4")["crawl_report"]
    assert "discovery page budget reached" in report["stop_reasons"]


def test_missing_sitemap_falls_back_to_links(tmp_path):
    def routes(base):
        page = "text/html"
        return {"/": (page, html("Home", ["/a/one", "/a/two", "/a/three", "/about"])),
                **{f"/a/{n}": (page, html(n, ["/"])) for n in ("one", "two", "three")},
                "/about": (page, html("About", []))}

    site = Site(routes)
    data = site.map(tmp_path)
    assert {"/sitemap.xml", "/sitemap_index.xml"} <= set(site.requested)  # common locations tried
    assert "/a/{segment_2}" in {r["pattern"] for r in data["routes"]}
    assert any("no sitemap declared" in x for x in data["crawl_report"]["limitations"])


def test_javascript_shell_is_reported(tmp_path):
    site = Site(lambda base: {"/": ("text/html", '<html><body><div id="root"></div><script src="/app.js"></script></body></html>')})
    data = site.map(tmp_path)
    assert any("JavaScript shell" in x for x in data["crawl_report"]["limitations"])


def test_robots_sitemap_lines_are_read_in_any_case():
    assert robots_sitemaps("User-agent: *\nSITEMAP: /a.xml\nsitemap:https://x.test/b.xml\n", "https://x.test/") == [
        "https://x.test/a.xml", "https://x.test/b.xml"]


def test_gzip_sitemap_over_the_byte_limit_is_refused():
    body = gzip.compress(URLSET.format("<url><loc>https://x.test/" + "a" * 5000 + "</loc></url>").encode())
    with pytest.raises(SitemapError, match="over"):
        read_sitemap(body, 1000)
    assert read_sitemap(body, 100_000)[0] == "urlset"


def test_xml_entity_expansion_is_refused():
    bomb = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><urlset><url><loc>&b;</loc></url></urlset>'
    with pytest.raises(SitemapError):
        read_sitemap(bomb.encode(), 100_000)


def test_actions_and_assets_are_recognized():
    assert is_action("https://x.test/account/logout") and is_action("https://x.test/cart/add?id=3")
    assert is_action("https://x.test/?add-to-cart=12") and not is_action("https://x.test/product/remover-tool-x")
    assert is_asset("https://x.test/a/b.PDF") and not is_asset("https://x.test/product/item.html")


def test_links_deeper_than_max_depth_are_not_fetched(tmp_path):
    chain = ["/", "/d1", "/d2", "/d3", "/d4"]
    site = Site(lambda base: {p: ("text/html", html(p, [chain[i + 1]] if i + 1 < len(chain) else []))
                              for i, p in enumerate(chain)})
    data = site.map(tmp_path, "--max-depth", "2")
    assert "/d2" in site.requested and "/d3" not in site.requested
    assert any(u["crawl_url"].endswith("/d3") for u in data["urls"])  # still discovered, as a link

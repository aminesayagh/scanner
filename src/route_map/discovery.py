"""Discovery: a bounded URL inventory from one seed URL, without fetching every page.

robots.txt is read for its sitemap declarations (and obeyed); without any, a few common sitemap locations
are tried. Sitemaps and sitemap indexes are followed (seen set, file count, byte and decompression limits)
and their URLs go into the inventory only. HTML pages are fetched from the seed outward, within the page
budget, preferring links to path shapes not seen yet over more members of a known family. Forms are never
submitted, and action links (logout, add to cart, delete) and binary assets are never requested.

The parsing and filtering helpers are plain functions; only `discover` runs Scrapy (once per process).
"""

import gzip
import io
import re
from urllib.parse import urljoin, urlsplit

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .models import Config

COMMON_SITEMAPS = ["/sitemap.xml", "/sitemap_index.xml"]
ASSET_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp", ".tif", ".tiff", ".pdf", ".zip",
    ".gz", ".rar", ".7z", ".mp3", ".mp4", ".webm", ".mov", ".avi", ".wav", ".ogg", ".css", ".js", ".mjs", ".json",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".exe", ".dmg", ".apk", ".doc", ".docx", ".xls", ".xlsx", ".ppt",
    ".pptx", ".csv", ".txt", ".rss", ".atom",
}
_ACTION = re.compile(
    r"(^|[/_\-?&=])(log-?out|sign-?out|logoff|add-to-cart|add_to_cart|cart/add|cart/change|cart/clear|remove|"
    r"delete|unsubscribe)([/_\-?&=.]|$)", re.I,
)
_SOFT_404 = re.compile(r"\b(404|not found|page introuvable|no encontrada|access denied|just a moment|attention required)\b", re.I)


class SitemapError(ValueError):
    pass


def robots_sitemaps(text: str, base: str) -> list[str]:
    """The `Sitemap:` declarations of a robots.txt (any case), resolved against the site."""
    out = []
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "sitemap" and value.strip():
            out.append(urljoin(base, value.strip()))
    return out


def read_sitemap(body: bytes, max_bytes: int) -> tuple[str, list[str]]:
    """("urlset" | "sitemapindex", the <loc> values) of a sitemap file, gzipped or not. Raises SitemapError
    for malformed XML, an unknown root, or a file over `max_bytes` once decompressed."""
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.GzipFile(fileobj=io.BytesIO(body)).read(max_bytes + 1)
        except (OSError, EOFError) as e:
            raise SitemapError(f"bad gzip: {e}") from e
    if len(body) > max_bytes:
        raise SitemapError(f"over {max_bytes} bytes")
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, DefusedXmlException) as e:
        raise SitemapError(f"malformed XML: {e}") from e
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in ("urlset", "sitemapindex"):
        raise SitemapError(f"unknown root <{kind}>")
    locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text and el.text.strip()]
    return kind, locs


def is_action(url: str) -> bool:
    parts = urlsplit(url)
    return bool(_ACTION.search(parts.path + ("?" + parts.query if parts.query else "")))


def is_asset(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(path.endswith(ext) for ext in ASSET_EXTENSIONS)


def path_shape(url: str) -> tuple:
    """A page's family guess for crawl priority: its path with the last segment generalized."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    return (len(segments), tuple(segments[:-1]), tuple(sorted(k for k, _, _ in (p.partition("=") for p in parts.query.split("&") if p))))


def host_allowed(host: str, allowed: str, subdomains: bool) -> bool:
    return host == allowed or (subdomains and host.endswith("." + allowed))


def looks_unsuitable(status: int, title: str | None) -> bool:
    """A 200 that is really an error, sign-in or challenge page: not usable to validate a route."""
    return status != 200 or bool(title and _SOFT_404.search(title))


def discover(seed: str, config: Config) -> dict:
    """Runs the crawl (blocking) and returns {"urls": [(url, source)], "redirects": {url: final},
    "observations": [PageObservation], "report": CrawlReport fields}."""
    from scrapy.crawler import AsyncCrawlerProcess

    from .spider import DiscoverySpider

    page_budget = max(1, config.max_html_pages // 2)  # ponytail: half for discovery, half kept for validation
    process = AsyncCrawlerProcess(settings={
        "ROBOTSTXT_OBEY": True,
        "USER_AGENT": "route-map (route inference; respects robots.txt)",
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.25,
        "AUTOTHROTTLE_MAX_DELAY": 10,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "DOWNLOAD_TIMEOUT": 20,
        "RETRY_TIMES": 2,
        "DOWNLOAD_MAXSIZE": config.max_sitemap_bytes,
        "CLOSESPIDER_TIMEOUT": config.deadline_seconds,
        "LOG_LEVEL": "WARNING",
        "TELNETCONSOLE_ENABLED": False,  # no listening ports: this is a one-shot command
        "REMOTE_CONTROL_ENABLED": False,
    })
    crawler = process.create_crawler(DiscoverySpider)
    process.crawl(crawler, seed=seed, config=config, page_budget=page_budget)
    process.start()
    return crawler.spider.result()


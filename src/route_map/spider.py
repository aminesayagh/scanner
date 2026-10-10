"""The Scrapy spider behind `discovery.discover`: an explicit scheduler for robots.txt, sitemaps and a
bounded set of pages. Sitemap entries are collected, never scheduled as page requests."""

import heapq
import itertools
from urllib.parse import urljoin, urlsplit

from scrapy import Request, Spider
from scrapy.exceptions import IgnoreRequest
from scrapy.spidermiddlewares.httperror import HttpError

from .discovery import (
    COMMON_SITEMAPS, SitemapError, host_allowed, is_action, is_asset, looks_unsuitable, path_shape, read_sitemap,
    robots_sitemaps,
)
from .models import Config, PageObservation
from .normalization import Rejected, normalize, url_id


class DiscoverySpider(Spider):
    name = "route-map-discovery"

    def __init__(self, seed: str, config: Config, page_budget: int, **kwargs):
        super().__init__(**kwargs)
        self.seed = normalize(seed)["crawl_url"]
        self.config, self.page_budget = config, page_budget
        self.host = urlsplit(self.seed).hostname
        self.allowed_domains = [self.host]
        self.pairs: set[tuple[str, str]] = set()
        self.known: set[str] = set()  # crawl URLs in the inventory
        self.sitemaps: set[str] = set()
        self.pages: set[str] = set()  # requested (each uses one slot of the page budget)
        self.queued: set[str] = set()
        self.frontier: list[tuple] = []  # (shape already fetched, order, url, depth)
        self.order = itertools.count()
        self.shapes: set[tuple] = set()
        self.observations: list[PageObservation] = []
        self.redirects: dict[str, str] = {}
        self.sitemap_results: list[dict] = []
        self.skipped, self.blocked, self.failed = set(), set(), set()
        self.stop_reasons: set[str] = set()
        self.limitations: list[str] = []
        self.counts = {"sitemap_files": 0, "sitemap_bytes": 0, "requests_used": 0}

    # --- scheduling ----------------------------------------------------------------------------------

    async def start(self):
        yield Request(urljoin(self.seed, "/robots.txt"), self.parse_robots, errback=self.robots_failed,
                      meta={"dont_obey_robotstxt": True}, priority=100, dont_filter=True)
        self.enqueue(self.seed, 0)
        for request in self.dispatch():
            yield request

    def add(self, url: str, source: str) -> bool:
        """Puts a URL in the inventory; False once the discovered-URL budget is used up."""
        try:
            crawl = normalize(url)["crawl_url"]
        except Rejected:
            return True
        host = urlsplit(crawl).hostname
        if not host_allowed(host, self.host, self.config.include_subdomains):
            self.skipped.add(host)
            return True
        if crawl not in self.known:
            if len(self.known) >= self.config.max_discovered_urls:
                self.stop_reasons.add("max_discovered_urls reached")
                return False
            self.known.add(crawl)
        self.pairs.add((url, source))
        return True

    def enqueue(self, url: str, depth: int) -> None:
        """A page waiting for a slot of the page budget (each slot goes to the best waiting page)."""
        try:
            crawl = normalize(url)["crawl_url"]
        except Rejected:
            return
        if crawl in self.queued or depth > self.config.max_depth:
            return
        if not host_allowed(urlsplit(crawl).hostname, self.host, self.config.include_subdomains):
            return
        self.queued.add(crawl)
        heapq.heappush(self.frontier, (path_shape(crawl) in self.shapes, next(self.order), crawl, depth))

    def dispatch(self):
        """Requests for waiting pages while the budget lasts: path shapes not fetched yet first (one more
        member of a known family teaches little), then in discovery order."""
        while self.frontier and len(self.pages) < self.page_budget:
            known, n, crawl, depth = heapq.heappop(self.frontier)
            if known != (path_shape(crawl) in self.shapes):  # its shape was fetched meanwhile
                heapq.heappush(self.frontier, (True, n, crawl, depth))
                continue
            self.pages.add(crawl)
            self.shapes.add(path_shape(crawl))
            yield Request(crawl, self.parse_page, errback=self.failed_page, meta={"route_depth": depth})
        if self.frontier:
            self.stop_reasons.add("discovery page budget reached")

    def sitemap(self, url: str) -> Request | None:
        if url in self.sitemaps:
            return None  # also stops cycles between sitemap indexes
        if len(self.sitemaps) >= self.config.max_sitemap_files:
            self.stop_reasons.add("max_sitemap_files reached")
            return None
        self.sitemaps.add(url)
        return Request(url, self.parse_sitemap, errback=self.sitemap_failed, priority=50, dont_filter=True)

    # --- callbacks -----------------------------------------------------------------------------------

    def parse_robots(self, response):
        declared = robots_sitemaps(response.text, self.seed)
        yield from self.first_sitemaps(declared)

    def robots_failed(self, failure):
        yield from self.first_sitemaps([])

    def first_sitemaps(self, declared: list[str]):
        if not declared:
            self.limitations.append("no sitemap declared in robots.txt: common locations tried")
        for url in declared or [urljoin(self.seed, path) for path in COMMON_SITEMAPS]:
            if request := self.sitemap(url):
                yield request

    def parse_sitemap(self, response):
        self.counts["sitemap_files"] += 1
        self.counts["sitemap_bytes"] += len(response.body)
        try:
            kind, locs = read_sitemap(response.body, self.config.max_sitemap_bytes)
        except SitemapError as e:
            self.sitemap_results.append({"url": response.url, "error": str(e)})
            return
        self.sitemap_results.append({"url": response.url, "kind": kind, "entries": len(locs)})
        for loc in locs:
            if kind == "sitemapindex":
                if request := self.sitemap(urljoin(response.url, loc)):
                    yield request
            elif not self.add(urljoin(response.url, loc), f"sitemap:{response.url}"):
                break

    def sitemap_failed(self, failure):
        status = failure.value.response.status if failure.check(HttpError) else None
        self.sitemap_results.append({"url": failure.request.url, "error": f"HTTP {status}" if status else repr(failure.value)})

    def parse_page(self, response):
        requested, final = response.request.meta.get("redirect_urls", [response.url])[0], response.url
        if final != requested:
            self.redirects[requested] = final
        if not host_allowed(urlsplit(final).hostname, self.host, self.config.include_subdomains):
            self.skipped.add(urlsplit(final).hostname)
            return
        self.add(requested, "page")
        if b"html" not in response.headers.get(b"Content-Type", b"text/html").lower():
            return
        title = response.css("title::text").get()
        links = []
        for href in response.css("a::attr(href)").getall():
            url = response.urljoin(href)
            try:
                crawl = normalize(url)["crawl_url"]
            except Rejected:
                continue
            if is_action(crawl) or is_asset(crawl):
                continue
            self.add(url, f"link:{final}")
            if host_allowed(urlsplit(crawl).hostname, self.host, self.config.include_subdomains):
                links.append(crawl)
                self.enqueue(crawl, response.meta["route_depth"] + 1)
        self.observations.append(PageObservation(
            url_id=url_id(normalize(final)["comparison_key"]),
            fetch_result="unsuitable" if looks_unsuitable(response.status, title) else "ok",
            status=response.status, final_url=final, title=title, links=sorted(set(links)),
        ))
        if normalize(final)["crawl_url"] == self.seed and not links:
            self.limitations.append("the seed page has no links in its HTML: it may be a JavaScript shell (not rendered in this version)")
        yield from self.dispatch()

    def failed_page(self, failure):
        if failure.check(IgnoreRequest) and not failure.check(HttpError):
            self.blocked.add(failure.request.url)  # disallowed by robots.txt
        else:
            self.failed.add(failure.request.url)
        yield from self.dispatch()  # the slot is spent; keep the crawl going

    def closed(self, reason):
        self.counts["requests_used"] = self.crawler.stats.get_value("downloader/request_count", 0)
        if reason == "closespider_timeout":
            self.stop_reasons.add("deadline reached")

    def result(self) -> dict:
        return {
            "urls": sorted(self.pairs),
            "redirects": dict(sorted(self.redirects.items())),
            "observations": sorted(self.observations, key=lambda o: o.url_id),
            "report": {
                **self.counts, "discovery_html": len(self.pages), "fetched_urls": len(self.observations),
                "skipped_hosts": sorted(self.skipped), "blocked_urls": sorted(self.blocked),
                "failed_urls": sorted(self.failed), "sitemap_results": sorted(self.sitemap_results, key=lambda r: r["url"]),
                "stop_reasons": sorted(self.stop_reasons), "limitations": self.limitations,
            },
        }

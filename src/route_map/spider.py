"""The Scrapy spider behind `discovery.discover`: an explicit scheduler for robots.txt, sitemaps and a
bounded set of pages. Sitemap entries are collected, never scheduled as page requests.

Phases share the page budget. Discovery fetches pages from the seed outward (half the budget at most).
When it runs dry, validation fetches each family's planned samples, then a few more for families whose
samples disagreed, then (with `Config.templates`) rounds of pages from families still showing new templates
and pages in no family, with what is left."""

from urllib.parse import urljoin, urlsplit

from scrapy import Request, Spider, signals
from scrapy.exceptions import DontCloseSpider, IgnoreRequest
from scrapy.spidermiddlewares.httperror import HttpError

from .discovery import (
    COMMON_SITEMAPS, SitemapError, host_allowed, is_action, is_asset, looks_unsuitable, path_shape, read_sitemap,
    robots_sitemaps,
)
from .features import observe
from .inference import families, records_from
from .models import Config, PageObservation
from .normalization import Rejected, normalize, url_id
from .validation import plan_extra, plan_samples, plan_templates, template_queues


WAVE_OF_KNOWN_SHAPES = 4
REDIRECTS = [301, 302, 303, 307, 308]


class DiscoverySpider(Spider):
    name = "route-map-discovery"

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.idle, signal=signals.spider_idle)
        return spider

    def __init__(self, seed: str, config: Config, page_budget: int, **kwargs):
        super().__init__(**kwargs)
        self.phase = "discovery"  # then "samples", "extra samples" and "templates"
        self.samples: set[str] = set()
        self.saturation: dict[str, tuple[int, int, int]] = {}  # family pattern -> plan_templates state
        self.reach: dict[str, int] = {}  # URL id -> the shallowest depth it was found at (sitemaps: 0)
        self.seed = normalize(seed)["crawl_url"]
        self.config, self.page_budget = config, page_budget
        self.host = urlsplit(self.seed).hostname
        self.allowed_domains = [self.host]
        self.pairs: set[tuple[str, str]] = set()
        self.known: set[str] = set()  # crawl URLs in the inventory
        self.sitemaps: set[str] = set()
        self.pages: set[str] = set()  # requested during discovery
        self.page_requests = 0  # page requests issued in every phase: the page budget is counted on this
        self.queued: set[str] = set()
        self.frontier: list[tuple[str, int]] = []  # (url, depth) waiting for a wave
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
        for request in self.wave():
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
        """A page waiting for a discovery wave."""
        try:
            crawl = normalize(url)["crawl_url"]
        except Rejected:
            return
        if crawl in self.queued or depth > self.config.max_depth:
            return
        if not host_allowed(urlsplit(crawl).hostname, self.host, self.config.include_subdomains):
            return
        self.queued.add(crawl)
        self.frontier.append((crawl, depth))

    def wave(self) -> list[Request]:
        """The next wave of discovery pages, chosen once every page in flight has answered, so the choice
        does not depend on which response came back first. A wave takes one waiting page per path shape
        not fetched yet (one member of a family is enough to show it); when every waiting shape is known,
        it takes a few more members. Ties go to the shallower page, then the URL in sorted order.
        # ponytail: waves wait for the slowest page of each round; fine for a bounded crawl.
        """
        waiting = sorted(self.frontier, key=lambda item: (item[1], item[0]))  # (url, depth)
        picked, shapes = [], set(self.shapes)
        for crawl, depth in waiting:
            if path_shape(crawl) not in shapes:
                shapes.add(path_shape(crawl))
                picked.append((crawl, depth))
        if not picked:
            picked = waiting[:WAVE_OF_KNOWN_SHAPES]
        picked = picked[: max(0, self.page_budget - self.page_requests)]
        for crawl, depth in picked:
            self.frontier.remove((crawl, depth))
            self.pages.add(crawl)
            self.shapes.add(path_shape(crawl))
        self.page_requests += len(picked)
        if self.frontier and self.page_requests >= self.page_budget:
            self.stop_reasons.add("discovery page budget reached")
        return [self.page_request(crawl, depth) for crawl, depth in picked]

    def page_request(self, url: str, depth: int) -> Request:
        # Redirects come back as responses: recorded as an alias, the target queued like any link, and never
        # followed to another host.
        return Request(url, self.parse_page, errback=self.failed_page, dont_filter=True,
                       meta={"route_depth": depth, "dont_redirect": True, "dont_retry": True,  # a retry would be a page outside the budget
                             "handle_httpstatus_list": REDIRECTS})

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
            else:
                self.reached(urljoin(response.url, loc), 0)

    def sitemap_failed(self, failure):
        status = failure.value.response.status if failure.check(HttpError) else None
        self.sitemap_results.append({"url": failure.request.url, "error": f"HTTP {status}" if status else repr(failure.value)})

    def idle(self):
        """Nothing in flight: the next discovery wave, else the next phase (samples, extra samples, then
        rounds of template sampling until none is planned), or the end of the crawl."""
        if self.phase == "discovery" and (requests := self.wave()):
            for request in requests:
                self.crawler.engine.crawl(request)
            raise DontCloseSpider
        while self.phase != "templates":
            if self.phase == "extra samples" and not self.config.templates:
                return
            self.phase = {"discovery": "samples", "samples": "extra samples", "extra samples": "templates"}[self.phase]
            if self.phase != "templates":
                self.validate()
            # Nothing to fetch in this phase: try the next one now rather than after another idle wait.
        self.sample_templates()

    def planning(self):
        """(records, families, observations by id, records by id, pages left in the budget, ids requested
        without an observation). The last are pages that were not HTML: never planned again, so no slot of the
        budget is spent twice on one URL."""
        records, _ = records_from(sorted(self.pairs), self.config, self.seed)
        observed = {o.url_id: o for o in self.observations}
        budget = max(self.config.max_html_pages - self.page_requests, 0)
        requested = self.pages | self.samples
        not_html = {r.id for r in records if r.crawl_url in requested and r.id not in observed}
        return records, families(records, self.config), observed, {r.id: r for r in records}, budget, not_html

    def fetch(self, picked: list[str], by_id: dict) -> None:
        for uid in picked:
            self.samples.add(by_id[uid].crawl_url)
            self.page_requests += 1
            self.crawler.engine.crawl(self.page_request(by_id[uid].crawl_url, self.config.max_depth))
        raise DontCloseSpider  # Scrapy checks idleness again a few seconds later

    def validate(self) -> None:
        """Samples, or extra samples: fetched (DontCloseSpider) unless there is nothing to fetch."""
        _, candidates, observed, by_id, budget, not_html = self.planning()

        def plan(limit):
            if self.phase == "samples":
                return plan_samples(candidates, set(observed), limit, self.config.samples_per_family, not_html)
            return plan_extra(candidates, observed, by_id, self.config, limit, not_html)

        picked = plan(budget)
        if len(plan(10**9)) > len(picked):
            self.stop_reasons.add("page budget ran out during validation: some families stay provisional")
        if picked:
            self.fetch(picked, by_id)

    def sample_templates(self) -> None:
        """One round of template sampling (DontCloseSpider), or the end of the crawl, with a stop reason for
        each family the budget left open. Not planned: pages requested without an observation (not HTML), and
        pages in no family found only deeper than max_depth (links of sample pages, as in discovery)."""
        records, candidates, observed, by_id, budget, not_html = self.planning()
        grouped = {u for c in candidates for u in c.member_ids}
        skip = not_html | {r.id for r in records if r.id not in grouped and self.reach.get(r.id, 0) > self.config.max_depth}
        if budget:
            if picked := plan_templates(candidates, records, observed, self.config, self.saturation, budget, skip):
                self.fetch(picked, by_id)
            return
        open_families, single = template_queues(candidates, records, observed, self.config, self.saturation, skip)
        for c, unchecked in open_families:
            if len(unchecked) < len(c.member_ids):  # never checked at all: the validation reason covers it
                self.stop_reasons.add(f"budget ran out before {c.pattern} was saturated: "
                                      f"{len(unchecked)} of {len(c.member_ids)} members not checked")
        if single:
            self.stop_reasons.add(f"budget ran out before {len(single)} individual pages were checked")

    def reached(self, url: str, depth: int) -> None:
        try:
            uid = self.observation_id(url)
        except Rejected:
            return
        self.reach[uid] = min(depth, self.reach.get(uid, depth))

    def observation_id(self, url: str) -> str:
        return url_id(normalize(url, tracking=self.config.tracking_parameters)["comparison_key"])

    def parse_page(self, response):
        requested = final = response.url
        if response.status in REDIRECTS:
            location = response.headers.get(b"Location", b"").decode("latin-1")
            target = response.urljoin(location) if location else None
            self.observations.append(PageObservation(url_id=self.observation_id(requested), fetch_result="ok",
                                                     status=response.status, final_url=target))
            if target:
                self.redirects[requested] = target
                self.add(target, f"redirect:{requested}")
                self.reached(target, response.meta["route_depth"])
                if self.phase == "discovery":
                    self.enqueue(target, response.meta["route_depth"])
            return
        self.add(requested, "page")
        if b"html" not in response.headers.get(b"Content-Type", b"text/html").lower():
            return
        seen = observe(response.text, final)
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
            self.reached(url, response.meta["route_depth"] + 1)
            if host_allowed(urlsplit(crawl).hostname, self.host, self.config.include_subdomains):
                links.append(crawl)
                if self.phase == "discovery":  # links of sample pages join the inventory, not the crawl
                    self.enqueue(crawl, response.meta["route_depth"] + 1)
        self.observations.append(PageObservation(
            url_id=self.observation_id(requested),
            fetch_result="unsuitable" if looks_unsuitable(response.status, seen["title"]) else "ok",
            status=response.status, final_url=final, links=sorted(set(links)), **seen,
        ))
        if normalize(final)["crawl_url"] == self.seed and not links:
            self.limitations.append("the seed page has no links in its HTML: it may be a JavaScript shell (not rendered in this version)")

    def failed_page(self, failure):
        blocked = failure.check(IgnoreRequest) and not failure.check(HttpError)  # disallowed by robots.txt
        (self.blocked if blocked else self.failed).add(failure.request.url)
        status = failure.value.response.status if failure.check(HttpError) else None
        self.observations.append(PageObservation(url_id=self.observation_id(failure.request.url),
                                                 fetch_result="blocked" if blocked else "failed", status=status,
                                                 error=str(failure.value)[:200]))

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
                **self.counts, "discovery_html": len(self.pages), "validation_html": len(self.samples),
                "fetched_urls": sum(o.fetch_result != "blocked" for o in self.observations),
                "skipped_hosts": sorted(self.skipped), "blocked_urls": sorted(self.blocked),
                "failed_urls": sorted(self.failed), "sitemap_results": sorted(self.sitemap_results, key=lambda r: r["url"]),
                "stop_reasons": sorted(self.stop_reasons), "limitations": self.limitations,
            },
        }

"""Records of a route map, kept separate as the plan asks: URLs, page observations, structural candidates,
route families and the crawl report. The JSON output is versioned by SCHEMA_VERSION.
"""

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"

RouteStatus = Literal["validated_on_sample", "provisional", "individual", "unresolved"]
ParameterType = Literal["integer", "uuid", "hex", "slug", "string"]


class Config(BaseModel):
    """Budgets and grouping settings. Defaults are starting choices, not accuracy guarantees."""

    max_discovered_urls: int = Field(50_000, ge=1)
    max_html_pages: int = Field(100, ge=1)
    samples_per_family: int = Field(5, ge=1)
    max_sitemap_files: int = Field(100, ge=1)
    max_depth: int = Field(4, ge=0)
    min_distinct_values: int = Field(3, ge=2)  # before a varying segment becomes a parameter
    max_sitemap_bytes: int = Field(50_000_000, ge=1)  # per file, after decompression (the protocol's limit)
    deadline_seconds: int = Field(300, ge=1)  # the whole discovery
    include_subdomains: bool = False
    tracking_parameters: list[str] = Field(default_factory=lambda: [
        "utm_*", "gclid", "dclid", "fbclid", "msclkid", "yclid", "mc_cid", "mc_eid", "_ga", "_gl",
    ])


class URLRecord(BaseModel):
    id: str
    original_url: str
    crawl_url: str
    comparison_key: str  # identity: scheme-less, normalized host, tracking parameters removed
    sources: list[str] = []  # how it was discovered: "inventory", "sitemap:<file>", "link:<page>"
    aliases: list[str] = []  # other original URLs with the same identity
    path_segments: list[str]
    trailing_slash: bool
    query_pairs: list[tuple[str, str]]
    hash_route: bool = False  # "#/..." or "#!...": a client-side router this version does not follow
    redirect_to: str | None = None
    canonical: str | None = None
    route_id: str | None = None


class PageObservation(BaseModel):
    url_id: str
    fetch_result: Literal["ok", "failed", "blocked", "unsuitable"]
    status: int | None = None
    final_url: str | None = None
    title: str | None = None
    features: list[str] = []  # main-content structure features
    structured_data_types: list[str] = []
    breadcrumbs: list[str] = []
    links: list[str] = []
    error: str | None = None


class Parameter(BaseModel):
    name: str
    location: Literal["path", "query"]
    position: int  # path: 0-based segment index; query: index of the pair
    type: ParameterType


class RouteCandidate(BaseModel):
    pattern: str
    parameter: Parameter
    member_ids: list[str]
    distinct_values: int
    sample_ids: list[str]  # the deterministic sample plan for validation


class Evidence(BaseModel):
    kind: str
    value: str
    sample_support: str | None = None


class RouteRecord(BaseModel):
    id: str
    pattern: str
    status: RouteStatus
    role: str = "unknown"
    role_certainty: Literal["supported", "weak", "unknown"] = "unknown"
    parameters: list[Parameter] = []
    discovered_url_count: int
    successful_sample_count: int = 0
    inferred_unsampled_url_count: int = 0
    examples: list[str] = []
    sample_ids: list[str] = []
    evidence: list[Evidence] = []
    excluded_literals: list[str] = []
    exception_route_ids: list[str] = []


class CrawlReport(BaseModel):
    budgets: Config
    requests_used: int = 0
    sitemap_files: int = 0
    sitemap_bytes: int = 0
    discovery_html: int = 0  # page requests spent on discovery (validation gets the rest of max_html_pages)
    skipped_hosts: list[str] = []
    blocked_urls: list[str] = []  # disallowed by robots.txt
    discovered_urls: int = 0
    fetched_urls: int = 0
    rejected_urls: list[str] = []  # not HTTP(S), or another host
    failed_urls: list[str] = []
    sitemap_results: list[dict] = []
    stop_reasons: list[str] = []
    limitations: list[str] = []


class RouteMap(BaseModel):
    schema_version: str = SCHEMA_VERSION
    seed: str | None = None
    routes: list[RouteRecord]
    urls: list[URLRecord]  # the full inventory, each with its route
    observations: list[PageObservation] = []
    crawl_report: CrawlReport

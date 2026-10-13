"""Conservative URL identity: only what is certainly equivalent is merged.

Kept as written: path case, percent-encoded delimiters (%2F stays inside its segment), extensions, the
trailing slash, unknown query parameters with their order, blank values and repeated keys. Removed: the
scheme from the comparison key, default ports, ordinary anchors and known tracking parameters.
"""

import fnmatch
import hashlib
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


class Rejected(ValueError):
    """Not a URL this tool maps (not HTTP(S), or no host)."""


def normalize(url: str, base: str | None = None, tracking: list[str] = ()) -> dict:
    """The URLRecord fields of `url` (resolved against `base`), without id or sources."""
    parts = urlsplit(urljoin(base, url) if base else url)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        raise Rejected(url)
    host = parts.hostname.lower().rstrip(".")
    netloc = host if parts.port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{parts.port}"
    path = parts.path or "/"
    raw_pairs = [p for p in parts.query.split("&") if p] if parts.query else []
    kept = [p for p in raw_pairs if not _tracking(unquote_plus(p.split("=", 1)[0]), tracking)]
    query = "&".join(kept)
    # "#/route" and "#!route" carry a client-side route: flagged, not followed in this version.
    hash_route = parts.fragment.startswith(("/", "!"))
    crawl = urlunsplit((scheme, netloc, path, query, ""))
    segments = [s for s in path.split("/") if s]
    return {
        "original_url": url,
        "crawl_url": crawl,
        "comparison_key": urlunsplit(("", netloc, path, query, "")).lstrip("/"),
        "path_segments": segments,
        "trailing_slash": path.endswith("/") and path != "/",
        "query_pairs": [tuple(p.split("=", 1)) if "=" in p else (p, "") for p in kept],
        "hash_route": hash_route,
    }


def url_id(comparison_key: str) -> str:
    return "url_" + hashlib.sha1(comparison_key.encode()).hexdigest()[:12]


def host_of(record: dict) -> str:
    return urlsplit(record["crawl_url"]).netloc


def display_path(crawl_url: str) -> str:
    """How a page is shown in a route map: its path, plus the query when there is one."""
    u = urlsplit(crawl_url)
    return u.path + (f"?{u.query}" if u.query else "")


def _tracking(key: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(key.lower(), p) for p in patterns)

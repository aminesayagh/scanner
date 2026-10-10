"""A local HTTP site for route-map tests and evaluation: pages, redirects, statuses and random delays."""

import http.server
import json
import random
import subprocess
import sys
import threading
import time

URLSET = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</urlset>'
INDEX = '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</sitemapindex>'


def urlset(base, paths):
    return URLSET.format("".join(f"<url><loc>{base}{p}</loc></url>" for p in paths))


def index(base, paths):
    return INDEX.format("".join(f"<sitemap><loc>{base}{p}</loc></sitemap>" for p in paths))


def html(title, links):
    return f"<html><head><title>{title}</title></head><body>" + "".join(f'<a href="{h}">{h}</a>' for h in links) + "</body></html>"


def redirect(location, status=301):
    return ("redirect", location, status)


class Site:
    """`routes(base)` returns {path: (content type, body) | (content type, body, status) | redirect(...)}.
    Every requested path is logged. `jitter` (seconds) delays each answer at random, with `seed`."""

    def __init__(self, routes, jitter: float = 0.0, seed: int = 0):
        self.requested = []
        self.rng = random.Random(seed)
        site, lock = self, threading.Lock()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                with lock:
                    site.requested.append(self.path)
                    delay = site.rng.random() * jitter
                time.sleep(delay)
                found = site.pages.get(self.path)
                if found is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if found[0] == "redirect":
                    self.send_response(found[2])
                    self.send_header("Location", found[1])
                    self.end_headers()
                    return
                kind, body, status = found if len(found) == 3 else (*found, 200)
                body = body.encode() if isinstance(body, str) else body
                self.send_response(status)
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
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stderr
        return json.loads(out.read_text())

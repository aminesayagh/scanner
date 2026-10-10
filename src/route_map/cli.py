"""route-map CLI: discovers a site's URLs from the seed (robots.txt, sitemaps, a bounded set of pages) and
maps them to route families. --inventory maps a list of URLs instead, without fetching anything."""

from pathlib import Path

import typer
from pydantic import ValidationError

from .discovery import discover
from .export import summary as summarize
from .inference import infer
from .models import Config
from .normalization import Rejected, normalize

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    seed: str = typer.Argument(..., help="The website URL to map; other hosts are left out."),
    output: Path = typer.Option(Path("route-map.json"), "--output", "-o", help="Where to write the route map JSON."),
    inventory: Path | None = typer.Option(None, help="A file of URLs, one per line, mapped without fetching anything."),
    summary: Path | None = typer.Option(None, help="Also write a readable Markdown summary here."),
    max_discovered_urls: int = typer.Option(50_000),
    max_html_pages: int = typer.Option(100),
    samples_per_family: int = typer.Option(5),
    max_sitemap_files: int = typer.Option(100),
    max_depth: int = typer.Option(4),
    deadline_seconds: int = typer.Option(300, help="Stop discovery after this many seconds."),
    include_subdomains: bool = typer.Option(False, help="Also follow subdomains of the seed's host."),
) -> None:
    try:
        config = Config(max_discovered_urls=max_discovered_urls, max_html_pages=max_html_pages,
                        samples_per_family=samples_per_family, max_sitemap_files=max_sitemap_files, max_depth=max_depth,
                        deadline_seconds=deadline_seconds, include_subdomains=include_subdomains)
    except ValidationError as e:
        raise typer.BadParameter("; ".join(f"--{'-'.join(str(x) for x in err['loc']).replace('_', '-')}: {err['msg']}" for err in e.errors()))
    if inventory is not None:
        lines = [line.strip() for line in inventory.read_text().splitlines()]
        urls = [(line, "inventory") for line in lines if line and not line.startswith("#")][: config.max_discovered_urls]
        result = infer(urls, config, seed=seed)
    else:
        try:
            normalize(seed)
        except Rejected:
            raise typer.BadParameter(f"not an HTTP(S) URL: {seed}")
        typer.echo(f"Discovering {seed} …", err=True)
        found = discover(seed, config)
        result = infer(found["urls"], config, seed=seed, observations=found["observations"],
                       redirects=found["redirects"], report=found["report"])
    output.write_text(result.model_dump_json(indent=1))
    if summary is not None:
        summary.write_text(summarize(result))
    families = sum(r.status != "individual" for r in result.routes)
    typer.echo(f"{len(result.urls)} URLs → {len(result.routes)} routes ({families} families) → {output}")
    for reason in result.crawl_report.stop_reasons:
        typer.echo(f"Stopped early: {reason} (partial map)", err=True)

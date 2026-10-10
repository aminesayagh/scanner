"""route-map CLI. Milestone 1 maps a URL inventory offline (--inventory); discovery from the seed URL alone
(robots.txt, sitemaps, links) is milestone 2."""

from pathlib import Path

import typer
from pydantic import ValidationError

from .inference import infer
from .models import Config

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    seed: str = typer.Argument(..., help="The website URL to map; other hosts are left out."),
    output: Path = typer.Option(Path("route-map.json"), "--output", "-o", help="Where to write the route map JSON."),
    inventory: Path | None = typer.Option(None, help="A file of URLs, one per line, mapped without fetching anything."),
    max_discovered_urls: int = typer.Option(50_000),
    max_html_pages: int = typer.Option(100),
    samples_per_family: int = typer.Option(5),
    max_sitemap_files: int = typer.Option(100),
    max_depth: int = typer.Option(4),
) -> None:
    try:
        config = Config(max_discovered_urls=max_discovered_urls, max_html_pages=max_html_pages,
                        samples_per_family=samples_per_family, max_sitemap_files=max_sitemap_files, max_depth=max_depth)
    except ValidationError as e:
        raise typer.BadParameter("; ".join(f"--{'-'.join(str(x) for x in err['loc']).replace('_', '-')}: {err['msg']}" for err in e.errors()))
    if inventory is None:
        typer.echo("Discovery from the seed URL is not built yet (milestone 2): pass --inventory FILE.", err=True)
        raise typer.Exit(2)
    lines = [line.strip() for line in inventory.read_text().splitlines()]
    urls = [(line, "inventory") for line in lines if line and not line.startswith("#")][: config.max_discovered_urls]
    result = infer(urls, config, seed=seed)
    output.write_text(result.model_dump_json(indent=1))
    families = sum(r.status != "individual" for r in result.routes)
    typer.echo(f"{len(result.urls)} URLs → {len(result.routes)} routes ({families} families) → {output}")

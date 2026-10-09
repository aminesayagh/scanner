import math
from contextlib import nullcontext
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from .capture import capture as run_capture
from .candidates import generate
from .models import ElementSnapshot
from .normalize import normalize
from .overlay import write_overlay

app = typer.Typer(help="Capture a webpage, propose its major sections, review them, and export JSON.")
console = Console()


@app.callback()
def main() -> None:
    pass


@app.command()
def capture(
    url: str,
    output: Path = typer.Option(..., "--output", "-o", help="New run directory for this capture."),
    width: int = typer.Option(1440, min=1, help="Viewport width in CSS pixels."),
    height: int = typer.Option(900, min=1, help="Viewport height in CSS pixels."),
    scale: float = typer.Option(1.0, min=0.1, help="Device scale factor."),
    headed: bool = typer.Option(False, help="Show the browser window."),
    manual_ready: bool = typer.Option(
        False, help="Wait for you to press Enter before capturing (dismiss banners first). Needs --headed."
    ),
) -> None:
    """Visit URL once and save a screenshot and manifest into the run directory."""
    if not math.isfinite(scale):
        raise typer.BadParameter("must be a finite number.", param_hint="--scale")
    if manual_ready and not headed:
        raise typer.BadParameter("--manual-ready needs --headed so you can see the page.", param_hint="--manual-ready")

    def wait_for_operator() -> None:
        console.input("[bold]Prepare the page in the browser window, then press Enter to capture…[/]")

    # The spinner would hide the Enter prompt, so manual mode runs without it.
    spinner = nullcontext() if manual_ready else console.status(f"Capturing {url}")
    try:
        with spinner:
            m = run_capture(
                url,
                output,
                width=width,
                height=height,
                scale=scale,
                headed=headed,
                wait_for_operator=wait_for_operator if manual_ready else None,
            )
    except FileExistsError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(2)

    if m.status == "failed":
        console.print(f"[red]Capture failed:[/] {(m.error or 'unknown error').splitlines()[0]}")
        console.print(f"Details in {output / 'manifest.json'}")
        raise typer.Exit(1)

    color = "green" if m.status == "complete" else "yellow"
    console.print(f"[{color}]Capture {m.status}[/] → {output}  (id {m.capture_id})")
    if m.scroll and m.scroll.truncated_reason:
        console.print(f"[yellow]Scroll pass stopped early:[/] {m.scroll.truncated_reason}")
    if m.screenshot and not m.screenshot.layout_consistent:
        console.print("[yellow]Layout kept changing around the screenshot; capture marked inconsistent.[/]")
    timed_out = [k for k, v in m.readiness.model_dump(exclude={"manual"}).items() if v is False]
    if timed_out:
        console.print(f"[yellow]Readiness waits that timed out:[/] {', '.join(timed_out)}")


@app.command()
def overlay(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory from `capture`."),
    ids: list[str] = typer.Option([], "--id", help="Element ID to draw (repeatable). Default: the body's children."),
    group_ids: list[str] = typer.Option([], "--group", help="Visual group ID to draw instead (repeatable)."),
    regions: bool = typer.Option(False, "--regions", help="Draw the top-level candidate regions instead."),
) -> None:
    """Draw element or group boxes over the screenshot to check that geometry lines up."""
    if not (run / "elements.json").exists():
        console.print(f"[red]Error:[/] {run} has no elements.json; was the capture successful?")
        raise typer.Exit(2)
    if regions and not (run / "candidates.json").exists():
        console.print(f"[red]Error:[/] {run} has no candidates.json; run `section-scan detect {run}` first.")
        raise typer.Exit(2)
    if group_ids and not (run / "groups.json").exists():
        console.print(f"[red]Error:[/] {run} has no groups.json; run `section-scan detect {run}` first.")
        raise typer.Exit(2)
    try:
        path = write_overlay(run, ids, group_ids, regions)
    except ValueError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(2)
    console.print(f"Overlay written → {path}")


@app.command()
def detect(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory from `capture`."),
    depth: int = typer.Option(0, min=0, help="Also print the visual-group tree to this depth."),
) -> None:
    """Analyse saved capture evidence (no site visit): visual groups, then candidate regions and boundaries."""
    if not (run / "elements.json").exists():
        console.print(f"[red]Error:[/] {run} has no elements.json; was the capture successful?")
        raise typer.Exit(2)
    snapshot = ElementSnapshot.model_validate_json((run / "elements.json").read_text())
    groups = normalize(snapshot)
    (run / "groups.json").write_text(groups.model_dump_json(indent=1))

    by_id = {g.id: g for g in groups.groups}
    tags = {e.id: e.tag for e in snapshot.elements}
    tree = Tree(f"[bold]{run}[/] — {len(groups.groups)} groups from {len(snapshot.elements)} elements")

    def add(node: Tree, gid: str, level: int) -> None:
        g = by_id[gid]
        b = g.bounds
        label = f"{g.id} {g.kind} <{tags[g.primary_element_id]}> y={b.y:.0f} h={b.height:.0f} w={b.width:.0f}"
        if g.positioning != "flow":
            label += f" [blue]{g.positioning}[/]"
        child = node.add(label)
        if level < depth:
            for c in g.child_ids:
                add(child, c, level + 1)
        elif g.child_ids:
            child.add(f"[dim]… {len(g.child_ids)} more[/]")

    if depth:
        for g in groups.groups:
            if g.parent_id is None:
                add(tree, g.id, 0)
        console.print(tree)
    console.print(f"Groups written → {run / 'groups.json'}  ({len(groups.groups)} groups)")

    cands = generate(groups)
    (run / "candidates.json").write_text(cands.model_dump_json(indent=1))
    for note in cands.structure.notes:
        console.print(f"[dim]{note}[/]")
    regions = Table("Region", "Role", "Lvl", "Groups", "y", "Height", "Notes")
    for r in cands.regions:
        regions.add_row(
            r.id, r.role, str(r.level), " ".join(r.group_ids), f"{r.bounds.y:.0f}", f"{r.bounds.height:.0f}",
            "; ".join(r.notes),
        )
    console.print(regions)
    bounds = Table("Boundary", "Lvl", "Between", "y", "Box gap", "Content gap", "Backgrounds", "Spans", "Shared bg")
    for b in cands.boundaries:
        e = b.evidence
        bounds.add_row(
            b.id, str(b.level), f"{b.above_region_id} → {b.below_region_id}", f"{b.y:.0f}", f"{b.box_gap:.0f}",
            "?" if b.content_gap is None else f"{b.content_gap:.0f}",
            "changes" if e.background_change else "same" if e.background_change is False else "unknown",
            "yes" if e.foreground_spans else "no", e.shared_painted_ancestor or "",
        )
    console.print(bounds)
    for o in cands.overlaps:
        console.print(f"Overlap {o.region_ids[0]} × {o.region_ids[1]}: {o.kind}, {o.area:.0f} px²")
    console.print(f"Candidates written → {run / 'candidates.json'}")

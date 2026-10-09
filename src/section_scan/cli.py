import math
from contextlib import nullcontext
from pathlib import Path

import typer
from rich.console import Console
from rich.tree import Tree

from .capture import capture as run_capture
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
) -> None:
    """Draw element or group boxes over the screenshot to check that geometry lines up."""
    if not (run / "elements.json").exists():
        console.print(f"[red]Error:[/] {run} has no elements.json; was the capture successful?")
        raise typer.Exit(2)
    if group_ids and not (run / "groups.json").exists():
        console.print(f"[red]Error:[/] {run} has no groups.json; run `section-scan detect {run}` first.")
        raise typer.Exit(2)
    try:
        path = write_overlay(run, ids, group_ids)
    except ValueError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(2)
    console.print(f"Overlay written → {path}")


@app.command()
def detect(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory from `capture`."),
    depth: int = typer.Option(3, min=0, help="Group tree depth to print."),
) -> None:
    """Analyse saved capture evidence (no site visit). Currently: simplify elements into visual groups."""
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

    for g in groups.groups:
        if g.parent_id is None:
            add(tree, g.id, 0)
    console.print(tree)
    console.print(f"Groups written → {run / 'groups.json'}")

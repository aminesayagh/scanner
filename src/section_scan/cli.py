from contextlib import nullcontext
from pathlib import Path

import typer
from rich.console import Console

from .capture import capture as run_capture

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

import typer

app = typer.Typer(help="Capture a webpage, propose its major sections, review them, and export JSON.")


@app.callback()
def main() -> None:
    # ponytail: keeps Typer in multi-command mode until the first command (capture) lands
    pass

import json
import math
import os
import shlex
import tomllib
import webbrowser
from contextlib import nullcontext
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape as escape_markup
from rich.table import Column, Table
from rich.tree import Tree

from .capture import capture as run_capture
from .candidates import generate
from .detector import detect as detect_sections
from .evaluate import Annotation
from .evaluate import evaluate as evaluate_run
from . import classify as classify_mod
from . import export as export_mod
from . import review as review_mod
from .models import DetectorConfig, ElementSnapshot, ProposalSnapshot
from .normalize import normalize
from .breakpoints import BREAKPOINTS, site_runs
from .breakpoints import merge as merge_breakpoints
from .overlay import write_overlay
from .report import describe_limitation, report_fingerprint, write_report

app = typer.Typer(help="Capture a webpage, propose its major sections, review them, and export JSON.")
console = Console()


def load_dotenv(path: Path = Path(".env")) -> None:
    """KEY=VALUE lines from ./.env into the environment (e.g. TYPESAFE_API_KEY); real variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if value and key not in os.environ:
            os.environ[key] = value.strip("'\"")


@app.callback()
def main() -> None:
    load_dotenv()


@app.command()
def capture(
    url: str,
    output: Path = typer.Option(..., "--output", "-o", help="New directory for this capture."),
    width: int | None = typer.Option(None, min=1, help="Single viewport width (CSS px). Default: three breakpoints."),
    height: int | None = typer.Option(None, min=1, help="Single viewport height (CSS px)."),
    scale: float = typer.Option(1.0, min=0.1, help="Device scale factor."),
    headed: bool = typer.Option(False, help="Show the browser window."),
    manual_ready: bool = typer.Option(
        False, help="Wait for you to press Enter before capturing (dismiss banners first). Needs --headed."
    ),
) -> None:
    """Visit URL and save screenshots, elements and manifests.

    By default the page is loaded three times, as a laptop, a tablet and a phone would load it, into
    OUTPUT/laptop, OUTPUT/tablet and OUTPUT/mobile. --width/--height capture one viewport into OUTPUT.
    """
    if not math.isfinite(scale):
        raise typer.BadParameter("must be a finite number.", param_hint="--scale")
    if manual_ready and not headed:
        raise typer.BadParameter("--manual-ready needs --headed so you can see the page.", param_hint="--manual-ready")

    if width is None and height is None:
        targets = [(bp, output / bp, b.width, b.height, b.device) for bp, b in BREAKPOINTS.items()]
        if taken := [str(t[1]) for t in targets if (t[1] / "manifest.json").exists()]:
            console.print(f"[red]Error:[/] already captured: {', '.join(taken)}; choose a new --output")
            raise typer.Exit(2)
    else:
        targets = [(None, output, width or 1440, height or 900, None)]

    failed = False
    for bp, out, w, h, device in targets:
        label = f"{bp} ({w}×{h})" if bp else f"{w}×{h}"

        def wait_for_operator(label=label) -> None:
            console.input(f"[bold]{label}: prepare the page in the browser window, then press Enter to capture…[/]")

        # The spinner would hide the Enter prompt, so manual mode runs without it.
        spinner = nullcontext() if manual_ready else console.status(f"Capturing {url} as {label}")
        try:
            with spinner:
                m = run_capture(
                    url, out, width=w, height=h, scale=scale, headed=headed, device=device,
                    wait_for_operator=wait_for_operator if manual_ready else None,
                )
        except FileExistsError as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(2)
        failed |= not _print_capture(m, out, label)
    if failed:
        raise typer.Exit(1)


def _print_capture(m, out: Path, label: str) -> bool:
    if m.status == "failed":
        console.print(f"[red]{label}: capture failed:[/] {escape_markup((m.error or 'unknown error').splitlines()[0])}")
        console.print(f"Details in {out / 'manifest.json'}")
        return False
    color = "green" if m.status == "complete" else "yellow"
    console.print(f"[{color}]{label}: capture {m.status}[/] → {out}  (id {m.capture_id})")
    if m.scroll and m.scroll.truncated_reason:
        console.print(f"[yellow]Scroll pass stopped early:[/] {m.scroll.truncated_reason}")
    if m.screenshot and not m.screenshot.layout_consistent:
        console.print("[yellow]Layout kept changing around the screenshot; capture marked inconsistent.[/]")
    timed_out = [k for k, v in m.readiness.model_dump(exclude={"manual"}).items() if v is False]
    if timed_out:
        console.print(f"[yellow]Readiness waits that timed out:[/] {', '.join(timed_out)}")
    return True


@app.command()
def overlay(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory from `capture`."),
    ids: list[str] = typer.Option([], "--id", help="Element ID to draw (repeatable). Default: the body's children."),
    group_ids: list[str] = typer.Option([], "--group", help="Visual group ID to draw instead (repeatable)."),
    regions: bool = typer.Option(False, "--regions", help="Draw the top-level candidate regions instead."),
    sections: bool = typer.Option(False, "--sections", help="Draw the proposed sections instead."),
) -> None:
    """Draw element or group boxes over the screenshot to check that geometry lines up."""
    if not (run / "elements.json").exists():
        console.print(f"[red]Error:[/] {run} has no elements.json; was the capture successful?")
        raise typer.Exit(2)
    if sections and not (run / "proposals.json").exists():
        console.print(f"[red]Error:[/] {run} has no proposals.json; run `section-scan detect {run}` first.")
        raise typer.Exit(2)
    if regions and not (run / "candidates.json").exists():
        console.print(f"[red]Error:[/] {run} has no candidates.json; run `section-scan detect {run}` first.")
        raise typer.Exit(2)
    if group_ids and not (run / "groups.json").exists():
        console.print(f"[red]Error:[/] {run} has no groups.json; run `section-scan detect {run}` first.")
        raise typer.Exit(2)
    try:
        path = write_overlay(run, ids, group_ids, regions, sections)
    except ValueError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(2)
    console.print(f"Overlay written → {path}")


@app.command()
def detect(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory from `capture`."),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False, help="detector.toml"),
    depth: int = typer.Option(0, min=0, help="Also print the visual-group tree to this depth."),
) -> None:
    """Analyse saved capture evidence (no site visit) and propose the page's major sections."""
    if config is None and Path("detector.toml").is_file():
        config = Path("detector.toml")  # a detector.toml in the working directory applies by default
    if config is not None:
        console.print(f"[dim]Using config {config}[/]")
    try:
        cfg = DetectorConfig.model_validate(tomllib.loads(config.read_text())) if config else DetectorConfig()
    except (tomllib.TOMLDecodeError, ValidationError) as e:
        console.print(f"[red]Invalid config {config}:[/] {e}")
        raise typer.Exit(2)
    if runs := site_runs(run):
        for bp, path in runs.items():
            console.rule(f"{bp}")
            _detect_one(path, cfg, depth)
        return
    _detect_one(run, cfg, depth)


def _detect_one(run: Path, cfg: DetectorConfig, depth: int) -> None:
    if not (run / "elements.json").exists():
        console.print(f"[red]Error:[/] {run} has no elements.json; was the capture successful?")
        raise typer.Exit(2)
    snapshot = ElementSnapshot.model_validate_json((run / "elements.json").read_text())
    groups = normalize(snapshot, cfg)
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

    cands = generate(groups, cfg)
    (run / "candidates.json").write_text(cands.model_dump_json(indent=1))
    for note in cands.structure.notes:
        console.print(f"[dim]{note}[/]")
    proposals = detect_sections(snapshot, groups, cands, cfg)
    (run / "proposals.json").write_text(proposals.model_dump_json(indent=1))

    table = Table("Boundary", "Lvl", "Between", "y", "Gap", "sA", "sB", "Ratio", "Decision", "Basis")
    fmt = lambda v: "?" if v is None else f"{v:.0f}"  # noqa: E731
    colour = {"split": "green", "keep": "dim", "ambiguous": "yellow"}
    for b in proposals.boundaries:
        table.add_row(
            b.id, str(b.level), f"{b.above_region_id} → {b.below_region_id}", f"{b.y:.0f}", fmt(b.gap),
            fmt(b.spacing_above.value), fmt(b.spacing_below.value),
            "?" if b.gap_ratio is None else f"{b.gap_ratio:.2f}",
            f"[{colour[b.decision]}]{b.decision}[/]", b.basis,
        )
    console.print(table)
    sections = Table("Section", "Label", "Role", "y", "Height", "Regions", "Flags")
    for s in proposals.sections:
        flags = ["[yellow]ambiguous[/]"] if s.ambiguous else []
        flags += [f"[yellow]hidden content {x.top:.0f}–{x.bottom:.0f}[/]" for x in s.limitations]
        flags += [f"overlaps {o} ({k})" for o, k in s.overlaps]
        sections.add_row(
            s.id, s.label, s.role, f"{s.bounds.y:.0f}", f"{s.bounds.height:.0f}", " ".join(s.region_ids),
            "; ".join(flags),
        )
    console.print(sections)
    for note in proposals.notes:
        console.print(f"[dim]{note}[/]")
    if proposals.status != "ok":
        console.print(f"[yellow]Analysis {proposals.status}[/]")
    console.print(f"Candidates → {run / 'candidates.json'};  proposals → {run / 'proposals.json'}")


@app.command()
def report(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory with proposals.json."),
    open_: bool = typer.Option(False, "--open", help="Open the report in a browser."),
) -> None:
    """Write a self-contained HTML report of the sections (reviewed state if a review exists).

    For a three-breakpoint capture: one report per breakpoint, and the merged report.json in RUN.
    """
    if runs := site_runs(run):
        missing = [str(p) for p in runs.values() if not (p / "proposals.json").exists()]
        if missing:
            console.print(f"[red]Error:[/] no proposals.json in {', '.join(missing)}; run `section-scan detect {run}` first.")
            raise typer.Exit(2)
        for bp, path in runs.items():
            _report_one(path, open_=False)
        doc = merge_breakpoints(runs)
        (run / "report.json").write_text(doc.model_dump_json(indent=2, exclude_defaults=True))
        console.print(f"Merged report → {run / 'report.json'}  ({len(doc.sections)} sections, {', '.join(runs)})")
        if open_:
            webbrowser.open((runs[next(iter(runs))] / "report.html").resolve().as_uri())
        return
    _report_one(run, open_)


def _report_one(run: Path, open_: bool) -> None:
    if not (run / "proposals.json").exists():
        console.print(f"[red]Error:[/] {run} has no proposals.json; run `section-scan detect {run}` first.")
        raise typer.Exit(2)
    state = review_mod.load(run)
    if state is not None and state.proposal_revision != review_mod.proposal_revision(run):
        console.print("[yellow]The review predates the current proposals; reporting the proposals only. "
                      "Run `review --restart` to review them.[/]")
        state = None
    path = write_report(run, state)
    console.print(f"Report written → {path} and {path.with_name('report.json')}")
    if open_:
        webbrowser.open(path.resolve().as_uri())


REVIEW_HELP = """Commands:
  list                                   show regions and their status
  inspect ID                             region details and the evidence behind its boundaries
  rename ID LABEL                        name a region (keeps its confirmation)
  merge ID ID                            merge two adjacent flow regions
  split ID Y|BOUNDARY [crossing=above|below]    cut a flow region at a document y or candidate boundary
  adjust ID [top=Y] [bottom=Y]           trim a flow region's empty edges
  exclude ID                             leave a region (e.g. an overlay) out of the export
  confirm ID | confirm all               confirm one region, or every retained region after `report`
  undo                                   restore the previous revision
  report                                 regenerate report.html for the current revision
  finish                                 leave the review (progress is already saved)"""


@app.command()
def review(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory with proposals.json."),
    command: list[str] = typer.Argument(None, help="Run one review command and exit; omit for interactive."),
    restart: bool = typer.Option(False, "--restart", help="Archive the current review and start from the latest proposals."),
) -> None:
    """Inspect, correct and confirm the proposed sections. Every change is saved immediately."""
    if not (run / "proposals.json").exists():
        console.print(f"[red]Error:[/] {run} has no proposals.json; run `section-scan detect {run}` first.")
        raise typer.Exit(2)
    state = review_mod.load(run)
    current = review_mod.proposal_revision(run)
    if state is not None and state.proposal_revision != current:
        if not restart:
            console.print(
                f"[yellow]The proposals changed since this review started ({state.proposal_revision} → {current}).[/]\n"
                "Confirmed corrections are not carried over silently. Use --restart to archive this review "
                "and review the new proposals."
            )
            raise typer.Exit(2)
        console.print(f"Archived previous review → {review_mod.archive(run, state)}")
        state = None
    if state is None:
        state = review_mod.start(run)
        review_mod.save(run, state, None)
        console.print(f"Started a review of {len(state.regions)} proposed regions.")

    elements = ElementSnapshot.model_validate_json((run / "elements.json").read_text())
    if command:
        _review_step(run, state, command, elements)
        return
    console.print(REVIEW_HELP)
    _review_list(state)
    while True:
        try:
            line = console.input(f"[bold]review r{state.revision}>[/] ")
        except (EOFError, KeyboardInterrupt):
            break
        try:
            words = shlex.split(line) if line.strip() else []
        except ValueError as e:  # e.g. an unmatched quote
            console.print(f"[red]Not applied:[/] {e}")
            continue
        if words and words[0] in ("finish", "quit", "exit"):
            break
        if words:
            state = _review_step(run, state, words, elements)
    outstanding = [r.id for r in state.regions if r.status == "proposed"]
    console.print(f"Review saved at revision {state.revision}. Unconfirmed: {', '.join(outstanding) or 'none'}.")


def _review_step(run: Path, state, words: list[str], elements: ElementSnapshot):
    """Apply one command; on any error print it and keep the previous state."""
    op, args = words[0], words[1:]
    try:
        if op == "help":
            console.print(REVIEW_HELP)
            return state
        if op == "list":
            _review_list(state)
            return state
        if op == "inspect":
            _review_inspect(run, state, _one(args))
            return state
        if op == "undo":
            state = review_mod.undo(run)
            console.print(f"Restored revision {state.revision}.")
            return state
        if op == "report":
            console.print(f"Report written → {write_report(run, state)}")
            return state
        if op == "rename" and len(args) >= 2:
            new = review_mod.rename(state, args[0], " ".join(args[1:]))
        elif op == "merge" and len(args) == 2:
            new = review_mod.merge(state, *args)
        elif op == "split" and len(args) in (2, 3):
            crossing = None
            if len(args) == 3:
                if not args[2].startswith("crossing="):
                    raise review_mod.ReviewError("usage: split ID Y|BOUNDARY [crossing=above|below]")
                crossing = args[2].split("=", 1)[1]
            y, at = _split_point(run, args[1])
            new = review_mod.split(state, args[0], y, elements, crossing, at)
        elif op == "adjust" and len(args) >= 2:
            edges = dict(a.split("=", 1) for a in args[1:] if "=" in a)
            if not edges or set(edges) - {"top", "bottom"}:
                raise review_mod.ReviewError("usage: adjust ID [top=Y] [bottom=Y]")
            new = review_mod.adjust(state, args[0], elements, **{k: float(v) for k, v in edges.items()})
        elif op == "exclude":
            new = review_mod.exclude(state, _one(args))
        elif op == "confirm" and args == ["all"]:
            new = review_mod.confirm_all(state, report_fingerprint(run))
        elif op == "confirm":
            new = review_mod.confirm(state, _one(args))
        else:
            raise review_mod.ReviewError(f"unknown or malformed command {' '.join(words)!r}; try `help`")
    except (review_mod.ReviewError, ValueError) as e:
        console.print(f"[red]Not applied:[/] {escape_markup(str(e))}")
        return state
    review_mod.save(run, new, state)
    console.print(f"[green]Saved revision {new.revision}[/]: {escape_markup(new.corrections[-1].command)}")
    return new


def _one(args: list[str]) -> str:
    if len(args) != 1:
        raise review_mod.ReviewError("expected exactly one region ID")
    return args[0]


def _split_point(run: Path, value: str) -> tuple[float, str]:
    if value.startswith("b"):
        props = ProposalSnapshot.model_validate_json((run / "proposals.json").read_text())
        match = next((b for b in props.boundaries if b.id == value), None)
        if match is None:
            raise review_mod.ReviewError(f"no candidate boundary {value!r}")
        return match.y, value
    return float(value), ""


def _review_list(state) -> None:
    table = Table("ID", "Label", "Role", "Status", "y", "Height", "Flags")
    colour = {"confirmed": "green", "excluded": "dim", "proposed": "yellow"}
    for r in state.regions:
        flags = (["ambiguous boundary"] if r.ambiguous else []) + (["[yellow]hidden content[/]"] if r.limitations else [])
        table.add_row(
            escape_markup(r.id), escape_markup(r.label), r.role, f"[{colour[r.status]}]{r.status}[/]",
            f"{r.bounds.y:.0f}", f"{r.bounds.height:.0f}", "; ".join(flags),
        )
    console.print(table)


def _review_inspect(run: Path, state, rid: str) -> None:
    _, r = review_mod._find(state, rid)
    props = ProposalSnapshot.model_validate_json((run / "proposals.json").read_text())
    sections = {s.id: s for s in props.sections}
    boundaries = {b.id: b for b in props.boundaries}
    console.print(f"[bold]{escape_markup(r.id)}[/] {escape_markup(r.label)} — {r.role}, {r.status}; y {r.bounds.y:.0f}–{r.bounds.y + r.bounds.height:.0f}, "
                  f"{len(r.element_ids)} elements; from {', '.join(r.source_section_ids)}")
    for x in r.limitations:
        console.print(f"  [yellow]limitation:[/] {describe_limitation(x)}")
    for note in r.notes:
        console.print(f"  note: {escape_markup(note)}")
    for sid in r.source_section_ids:
        s = sections.get(sid)
        for side, bid in (("above", s.boundary_above_id if s else None), ("below", s.boundary_below_id if s else None)):
            if bid and (b := boundaries.get(bid)):
                ratio = "unknown" if b.gap_ratio is None else f"{b.gap_ratio:.2f}"
                console.print(f"  boundary {side} {b.id}: [bold]{b.decision}[/] ({b.basis}), gap ratio {ratio}")
                for label, xs in (("for", b.supporting), ("against", b.opposing), ("unknown", b.unknowns)):
                    for x in xs:
                        console.print(f"    {label}: {escape_markup(x)}")


@app.command()
def export(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory with a review."),
    output: Path = typer.Option(..., "--output", "-o", help="Where to write the JSON."),
    draft: bool = typer.Option(False, "--draft", help="Include unconfirmed regions, each with its status."),
) -> None:
    """Export the confirmed sections as JSON (a --draft may include unconfirmed ones, marked as such)."""
    if not (run / "proposals.json").exists():
        console.print(f"[red]Error:[/] {run} has no proposals.json; run `section-scan detect {run}` first.")
        raise typer.Exit(2)
    try:
        doc = export_mod.build(run, draft)
    except export_mod.ExportError as e:
        console.print(f"[red]Not exported:[/] {escape_markup(str(e))}")
        raise typer.Exit(1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(doc.model_dump_json(indent=2))
    console.print(f"Exported {len(doc.regions)} regions ({doc.kind}) → {output}")


@app.command()
def evaluate(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory from `capture`."),
    annotation: Path = typer.Argument(..., exists=True, dir_okay=False, help="Expected sections (JSON)."),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False, help="detector.toml"),
) -> None:
    """Compare the combined detector with container-only and gaps-only baselines on one annotated capture.

    Runs in memory: proposals.json and any review are left untouched.
    """
    if config is None and Path("detector.toml").is_file():
        config = Path("detector.toml")  # same default as `detect`, so a tuning loop measures what it tunes
    try:
        base = DetectorConfig.model_validate(tomllib.loads(config.read_text())) if config else DetectorConfig()
    except (tomllib.TOMLDecodeError, ValidationError) as e:
        console.print(f"[red]Invalid config {config}:[/] {escape_markup(str(e))}")
        raise typer.Exit(2)
    console.print(f"[dim]Config: {config or 'built-in defaults'} — {base.model_dump(exclude={'mode'})}[/]")
    ann = Annotation.model_validate_json(annotation.read_text())
    snapshot = ElementSnapshot.model_validate_json((run / "elements.json").read_text())
    groups = normalize(snapshot, base)
    cands = generate(groups, base)
    table = Table("Expected", "Proposed", "Missed", "False splits", "Extra", "Ambiguous", "Est. edits")
    table.columns.insert(0, Column("Mode", no_wrap=True, min_width=9))  # never truncate the mode name
    details = []
    for mode in ("combined", "container", "gaps"):
        props = detect_sections(snapshot, groups, cands, base.model_copy(update={"mode": mode}))
        r = evaluate_run(snapshot, props, ann)
        table.add_row(mode, str(r.expected_sections), str(r.proposed_sections), str(len(r.missed_boundaries)),
                      str(len(r.false_splits)), str(len(r.extra_sections)), str(r.ambiguous_sections),
                      str(r.estimated_edits))
        details += [f"{mode}: missed {m}" for m in r.missed_boundaries] + [f"{mode}: false split, {f}" for f in r.false_splits]
        details += [f"{mode}: extra section {x}" for x in r.extra_sections]
        if r.unresolved_anchors:
            details.append(f"anchors not found: {', '.join(r.unresolved_anchors)}")
        if r.unowned_anchors:
            details.append(f"{mode}: anchors in no proposed section: {', '.join(r.unowned_anchors)}")
    console.print(f"{ann.page}  (annotated by {ann.annotator})")
    console.print(table)
    for d in dict.fromkeys(details):
        console.print(f"  {escape_markup(d)}")


def make_typesafe_client():
    """The TypeSafe client (TYPESAFE_API_KEY from the environment); replaced in tests."""
    from typesafe_sdk import TypeSafeClient

    return TypeSafeClient()


@app.command()
def classify(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Directory holding report.json."),
    review_labels: bool = typer.Option(True, "--review/--no-review", help="Confirm each label after proposing."),
    hide_dom_id: bool = typer.Option(False, "--hide-dom-id", help="Leave DOM ids out of the input (evaluation)."),
) -> None:
    """Propose a label for every section with Jev, then the page's industry and scope (page type); confirm,
    change or leave each one unresolved.

    Sections come first, so the page is judged with your section labels. Progress is saved after every
    proposal and decision; rerunning resumes. Writes report-classified.json.
    """
    from typesafe_sdk import TypeSafeError

    report_path = run / "report.json"
    if not report_path.exists():
        console.print(f"[red]Error:[/] {run} has no report.json; run `section-scan report {run}` first.")
        raise typer.Exit(2)
    report_doc = json.loads(report_path.read_text())
    model = classify_mod.requested_model()

    def progress(key, p):
        what = f"{p.label} ({p.confidence:.2f})" if p.status == "proposed" and p.confidence is not None else p.label
        if p.status == "failed":
            what = f"[red]failed[/] {escape_markup(p.error or '')}"
        elif p.source == "preprocessing":
            what = f"{p.label} [dim]({p.reason}, no request)[/]"
        console.print(f"  {key}: {what}")

    console.print(f"[dim]Model {model}, label registry {classify_mod.REGISTRY_VERSION}[/]")
    try:
        state = classify_mod.ensure_proposals(run, report_doc, make_typesafe_client, model, hide_dom_id, progress)
    except TypeSafeError as e:
        console.print(f"[red]Cannot reach TypeSafe:[/] {escape_markup(str(e))}\nSet TYPESAFE_API_KEY (from the TypeSafe console) and rerun.")
        raise typer.Exit(2)

    sections_done = _review_labels(run, report_doc, state) if review_labels else True
    if sections_done:
        try:
            page = classify_mod.ensure_page_proposal(run, report_doc, state, make_typesafe_client, model)
        except TypeSafeError as e:
            console.print(f"[red]Cannot reach TypeSafe:[/] {escape_markup(str(e))}\nSet TYPESAFE_API_KEY (from the TypeSafe console) and rerun.")
            raise typer.Exit(2)
        if page.status == "failed":
            console.print(f"  page: [red]failed[/] {escape_markup(page.error or '')}")
        else:
            console.print("  page: " + ", ".join(f"{q} {a.label}" + (f" ({a.confidence:.2f})" if a.confidence is not None else "")
                                              for q, a in page.answers.items()))
        if review_labels:
            _review_page(run, report_doc, state)
    else:
        console.print("[dim]Industry and scope wait until every section is decided (next session).[/]")
    path = classify_mod.export(run, report_doc, state)
    statuses = [s.decision.status if s.decision else (s.proposal.status if s.proposal else "pending") for s in state.sections.values()]
    page = {q: (d.label or d.status) for q, d in state.page.decisions.items()}
    console.print(f"Classified report → {path}  (sections: {', '.join(f'{statuses.count(x)} {x}' for x in sorted(set(statuses)))}"
                  + (f"; page: {', '.join(f'{q} {v}' for q, v in page.items())})" if page else ")"))


def _review_labels(run: Path, report_doc: dict, state) -> bool:
    """Review undecided sections in page order. False when the reviewer quits early."""
    for i, section in enumerate(report_doc["sections"]):
        key = classify_mod.section_key(i)
        s = state.sections.get(key)
        if s is None or s.proposal is None or s.decision is not None:
            continue
        p = s.proposal
        heading, preview = _heading_and_preview(section["message"])
        console.rule(f"{key} · id {section.get('id') or '—'} · {section['role']}")
        console.print(f"[bold]{escape_markup(heading or '(no heading)')}[/]")
        if preview:
            console.print(f"[dim]{escape_markup(preview)}[/]")
        _show_proposal(p.status, p.source, p.label, p.probabilities, p.confidence, p.reason, p.error)
        choice = _ask_decision(classify_mod.LABELS, p.label if p.status == "proposed" else None)
        if choice is None:
            return False
        classify_mod.decide(run, state, key, *choice)
        d = state.sections[key].decision
        console.print(f"[green]Saved[/] {key}: {d.status} {d.label or ''}")
    return True


def _review_page(run: Path, report_doc: dict, state) -> None:
    p = state.page.proposal
    pending = [q for q in classify_mod.PAGE_QUESTIONS if q not in state.page.decisions]
    if p is None or not pending:
        return
    console.rule("page")
    console.print(f"[bold]{escape_markup(report_doc.get('title') or '(no title)')}[/] · {escape_markup(report_doc.get('link') or '')}")
    if report_doc.get("description"):
        console.print(f"[dim]{escape_markup(report_doc['description'])}[/]")
    labels = [classify_mod.section_label(state, i) or "?" for i in range(len(report_doc["sections"]))]
    console.print(f"[dim]Sections: {' · '.join(labels)}[/]")
    for q in pending:
        _, registry = classify_mod.PAGE_QUESTIONS[q]
        console.print(f"[bold]{q}[/]")
        a = p.answers.get(q)
        _show_proposal(p.status, p.source, a.label if a else None, a.probabilities if a else None,
                       a.confidence if a else None, p.reason, p.error)
        choice = _ask_decision(registry, a.label if p.status == "proposed" and a else None)
        if choice is None:
            return
        classify_mod.decide_page(run, state, q, *choice)
        d = state.page.decisions[q]
        console.print(f"[green]Saved[/] {q}: {d.status} {d.label or ''}")


def _show_proposal(status, source, label, probabilities, confidence, reason, error) -> None:
    if status == "failed":
        console.print(f"[red]No proposal:[/] {escape_markup(error or '')}")
    elif source == "preprocessing":
        console.print(f"Proposed: [bold]{label}[/] ({reason}; no request made)")
    else:
        ranked = sorted(probabilities.items(), key=lambda kv: -kv[1])
        alts = ", ".join(f"{name} {prob:.0%}" for name, prob in ranked[1:3])
        console.print(f"Proposed: [bold]{label}[/] {ranked[0][1]:.0%} (confidence {confidence:.2f}) · next: {alts}")


def _ask_decision(registry: dict, proposed: str | None) -> tuple[str, str | None] | None:
    """Accept, choose another or leave unresolved; None when the reviewer quits."""
    names = list(registry)
    while True:
        options = ("[a]ccept, " if proposed else "") + "[c]hoose another, [u]nresolved, [q]uit"
        try:
            answer = console.input(f"{options} > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer in ("q", "quit"):
            return None
        if answer in ("a", "accept") and proposed:
            return "confirmed", proposed
        if answer in ("u", "unresolved"):
            return "unresolved", None
        if answer in ("c", "choose"):
            for n, name in enumerate(names, 1):
                console.print(f"  {n:>2}. {name} — {escape_markup(registry[name]['what'])}")
            try:
                pick = console.input("label number or name > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            chosen = names[int(pick) - 1] if pick.isdigit() and 0 < int(pick) <= len(names) else pick
            if chosen in registry:
                return "confirmed", chosen
            console.print("[red]Not a label in the registry.[/]")


def _heading_and_preview(message: list) -> tuple[str | None, str]:
    flat = []

    def walk(entries):
        for e in entries:
            if "text" in e:
                flat.append(e)
            walk(e.get("children", []))

    walk(message)
    heading = next((e["text"] for e in flat if e["tag"] in ("h1", "h2", "h3", "h4", "h5", "h6")), None)
    preview = " · ".join(e["text"] for e in flat if e["text"] != heading)
    return heading, (preview[:160] + "…") if len(preview) > 160 else preview

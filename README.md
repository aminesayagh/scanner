# section-scan

A local Python CLI that captures a webpage, proposes its major sections with the reasons for each
boundary, lets you correct and confirm them, and exports the approved list as JSON. It detects regions and
boundaries only; it does not rebuild the layout inside each section.

## Setup

Requires Python 3.12 (managed by [uv](https://docs.astral.sh/uv/)) and a Chromium build for Playwright.

```bash
uv sync                                 # create .venv with the locked dependencies
uv run playwright install chromium      # browser used for capture (once per machine)
uv run section-scan --help
uv run pytest                           # full test suite (~2 minutes; uses Chromium)
```

To install the command elsewhere: `uv build`, then `pip install dist/section_scan-*.whl` in any Python
3.12+ environment, followed by `python -m playwright install chromium`.

## Workflow

```bash
section-scan capture https://example.com -o runs/example --width 1440 --height 900
section-scan detect runs/example            # uses ./detector.toml if present, or --config FILE
section-scan report runs/example --open     # self-contained HTML: regions + every boundary's evidence
section-scan review runs/example            # interactive; type `help`
section-scan export runs/example -o sections.json
```

Only `capture` visits the site. Everything after it works offline on the saved run directory, so
detection can be rerun (for example with a different `detector.toml`) without recapturing. The same capture
and config always give the same ordered result.

| Command | What it does |
|---|---|
| `capture URL -o DIR` | Loads the page once and saves `manifest.json`, `screenshot.png` and `elements.json`. It waits for fonts, images and layout within time limits, and scrolls within limits so lazy-loaded content appears. Add `--headed --manual-ready` to dismiss banners yourself before capture. |
| `detect DIR` | Writes `groups.json` (simplified visual groups), `candidates.json` (regions, boundaries and evidence) and `proposals.json` (decisions and proposed sections). |
| `report DIR` | Writes `report.html` for the reviewed state, or the raw proposals if there is no review yet. |
| `review DIR [CMD …]` | Interactive prompt, or one command: `list`, `inspect`, `rename`, `merge`, `split ID Y\|bN [crossing=above\|below]`, `adjust ID top=… bottom=…`, `exclude`, `confirm ID`, `confirm all`, `undo`, `report`, `finish`. |
| `export DIR -o FILE [--draft]` | Writes confirmed sections. A normal export fails until every retained region is confirmed; `--draft` includes unconfirmed regions marked `proposed`. |
| `overlay DIR [--id …\|--group …\|--regions\|--sections]` | Diagnostic: draws elements, groups, regions or sections over the screenshot. |
| `evaluate DIR ANNOTATION` | Compares the detector with container-only and gaps-only baselines (see `evaluation/README.md`). |

## Section policy

- A **section** is a major visual or functional region of a vertically flowing page. It may span several
  source elements. Cards and nested content groups are children of a section, not sections themselves.
- **Header and footer** landmarks are kept as their own sections. A `<header>` taller than 300 px or
  starting below 200 px is treated as content (often a hero banner).
- **Overlays** (fixed elements such as cookie banners, chat buttons and skip links) are separate sections
  you can `exclude` during review.
- **Labels are provisional** ("Section 3"). Naming a section "pricing" is your call, made with `rename`.
- A boundary is **split**, **kept**, or **ambiguous**. Ambiguous boundaries still cut the list, but the
  sections next to them are flagged, because merging in review is easier than splitting.
- **Nothing is confirmed automatically.** Confirmation is yours, per section or with `confirm all`, and
  `confirm all` only works after you have generated the report for exactly the current state.

## Tuning (`detector.toml`)

| Key | Default | Meaning |
|---|---|---|
| `gap_ratio_threshold` | 3.0 | Content gap ÷ local spacing at or above this counts as a strong gap |
| `fallback_spacing_floor` | 16 | Spacing floor in CSS px when no line height can be measured |
| `min_spacing_observations` | 2 | Internal gaps a side needs before its spacing estimate counts |
| `mode` | `combined` | `container` or `gaps` run the evaluation baselines |

These are experimental starting values, not validated constants. Unknown keys are rejected, and every
`proposals.json` records the full effective configuration.

## Limits

- **Pages:** one URL and one viewport per capture; ordinary vertically flowing desktop pages.
- **Not inspected:**
  - iframe contents, closed shadow roots and canvas pixels;
  - nested scroll areas, recorded as `scroll_container`;
  - scroll-driven effects. Content that only appears while scrolling may be missing from the screenshot.

  Uninspected areas are recorded as limitations, never treated as empty.
- **Screenshots:** a full-page screenshot is evidence of one moment, not of every dynamic state.
- **Element IDs:** they belong to one capture. A recapture gets a new capture ID, and approvals do not
  carry over to a changed page.
- **Re-running detection:** after a new detection run, review requires `--restart`. The old review is
  archived, not overwritten.
- **Inner alternatives:** they are explored one level deep, so a section nested two levels inside
  another region is not offered as a split.
- **Splitting and adjusting:** only flow sections can be split or adjusted. Content crossing a cut
  needs an explicit `crossing=` choice.
- **Single reviewer:** there's no locking, so don't review the same run from two terminals at once.

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

From the project folder, prefix commands with `uv run` (or activate `.venv`, or install the wheel):

```bash
uv run section-scan capture https://example.com -o runs/example --width 1440 --height 900
uv run section-scan detect runs/example            # uses ./detector.toml if present, or --config FILE
uv run section-scan report runs/example --open     # self-contained HTML: regions + every boundary's evidence
uv run section-scan review runs/example            # interactive; type `help`
uv run section-scan export runs/example -o sections.json
```

Only `capture` visits the site. Everything after it works offline on the saved run directory, so
detection can be rerun (for example with a different `detector.toml`) without recapturing. The same capture
and config always give the same ordered result.

| Command | What it does |
|---|---|
| `capture URL -o DIR` | Loads the page once and saves `manifest.json`, `screenshot.png` and `elements.json`. It waits for fonts, images and layout within time limits, and scrolls within limits so lazy-loaded content appears. Add `--headed --manual-ready` to dismiss banners yourself before capture. |
| `detect DIR` | Writes `groups.json` (simplified visual groups), `candidates.json` (regions, boundaries and evidence) and `proposals.json` (decisions and proposed sections). |
| `report DIR` | Writes `report.html` for the reviewed state, or the raw proposals if there is no review yet, and `report.json` (see below). |
| `review DIR [CMD …]` | Interactive prompt, or one command: `list`, `inspect`, `rename`, `merge`, `split ID Y\|bN [crossing=above\|below]`, `adjust ID top=… bottom=…`, `exclude`, `confirm ID`, `confirm all`, `undo`, `report`, `finish`. |
| `export DIR -o FILE [--draft]` | Writes confirmed sections in top-to-bottom `order`. A normal export fails until every retained region is confirmed; `--draft` includes unconfirmed regions marked `proposed`. |
| `overlay DIR [--id …\|--group …\|--regions\|--sections]` | Diagnostic: draws elements, groups, regions or sections over the screenshot. |
| `evaluate DIR ANNOTATION` | Compares the detector with container-only and gaps-only baselines (see `evaluation/README.md`); uses `./detector.toml` like `detect`. |

## report.json

A compact summary next to `report.html`, with sections top to bottom (excluded ones left out):

```json
{
  "link": "https://example.com/",
  "title": "Page title",
  "description": "Meta description",
  "sections": [
    {
      "id": "pricing",
      "position": { "x": 0, "y": 900, "width": 1440, "height": 982 },
      "flags": ["ambiguous", "hidden_content", "overlaps:overlay"],
      "role": "flow",
      "message": [
        { "tag": "group", "children": [ { "tag": "h2", "text": "Pricing" }, { "tag": "p", "text": "Pick one of these plans." } ] },
        { "tag": "ul", "children": [ { "tag": "li", "text": "Starter" }, { "tag": "li", "text": "Pro" } ] },
        { "tag": "p", "text": "Start free" }
      ]
    }
  ]
}
```

- **`id`:** the `id` attribute of the outermost element that wraps all of the section's visible content and
  nothing from another section, so `#id` scrolls to it; `null` when there is none.
- **`position`:** document-space CSS pixels.
- **`message`:** the section's visible text in page order, following the page's DOM branches.
  Wrappers around a single branch are looked through. A branch whose parts are single items (a heading
  and its paragraph, the links of one nav) becomes a `group`, or `ul`/`ol` for a list, with them as
  `children`; a lone item stays an item, and loose text becomes `p`. A branch whose parts hold several
  items themselves is split into those parts, so a group never contains another group. Headings,
  paragraphs and list items keep their tag (`h1`–`h6`, `p`, `li`); other text inside a group is `text`.
  A `group` with no siblings is unwrapped: its items become the message itself.
  A list item that holds headings or paragraphs of its own (a work entry: date, title, description) keeps
  them as its `children`: the only place a third level appears.
- **What text counts:** the DOM text, including parts not painted at capture (a closed `<details>`,
  content waiting for an animation). Left out: elements removed with `display: none` (typically duplicate
  mobile menus) and screen-reader-only text.
- **Older captures:** captures made before extractor 3 have no `id`, title or description, and only
  80-character text previews.

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

Lengths are CSS px; shares are fractions of a width or height, in (0, 1].

| Group | Key | Default | Meaning |
|---|---|---|---|
| Structure | `geometry_tolerance` | 2 | Edges this close (px) count as the same box |
| | `touch_tolerance` | 2 | Overlaps this small (px) count as touching, not crossing |
| | `max_nesting` | 3 | Deepest level of nested alternative regions below a top-level region |
| | `full_width` | 0.9 | Width share that counts as full width (fallback header, dominant paint, page-wide paint change) |
| | `dominant_paint_height` | 0.5 | Height share a painted descendant needs to colour its region |
| Headers & footers | `header_max_top` | 200 | A header landmark must start within this distance of the top... |
| | `header_max_height` | 300 | ...and be at most this tall; otherwise it is content (often a hero) |
| | `top_edge` | 10 | A fallback (non-landmark) header must start this close to the top |
| | `footer_max_gap` | 100 | A footer landmark must end this close to the page bottom... |
| | `footer_min_width` | 0.5 | ...and span at least this share of the page width |
| Spacing | `gap_ratio_threshold` | 3.0 | Content gap ÷ local spacing at or above this counts as a strong gap |
| | `fallback_spacing_floor` | 16 | Spacing floor in CSS px when no line height can be measured |
| | `min_spacing_observations` | 2 | Internal gaps a side needs before its spacing estimate counts |
| | `spacing_depth` | 2 | Stacking levels of rows that internal spacing is measured in |
| Limitations | `hidden_min_height` | 300 | A band of invisible captured content this tall is flagged `hidden_content` |
| Decisions | `mode` | `combined` | `container` or `gaps` run the evaluation baselines |

These are experimental starting values, not validated constants. Out-of-range values and unknown keys
are rejected. `groups.json`, `candidates.json` and `proposals.json` each record the full effective
configuration.

## Limits

- **Pages:** one URL and one viewport per capture; ordinary vertically flowing desktop pages.
- **Not inspected:**
  - iframe contents, closed shadow roots and canvas pixels;
  - nested scroll areas, recorded as `scroll_container`;
  - scroll-driven effects. Content that only appears while scrolling may be missing from the screenshot.
    A tall band of a section holding only such invisible content is recorded on the section as a
    `hidden_content` limitation.

  Uninspected areas are recorded as limitations, never treated as empty.
- **Covered content:** an element hidden under another opaque element (for example a fixed header) is
  detected only inside the viewport at capture time. Elsewhere on the page, whether it is covered is
  recorded as unknown, and the element counts as visible.
- **Screenshots:** a full-page screenshot is evidence of one moment, not of every dynamic state.
- **Element IDs:** they belong to one capture. A recapture gets a new capture ID, and approvals do not
  carry over to a changed page.
- **Re-running detection:** after a new detection run, review requires `--restart`. The old review is
  archived, not overwritten.
- **Inner alternatives:** they are explored up to three levels deep (`MAX_NESTING` in candidates.py), so a
  section nested deeper inside another region is not offered as a split.
- **Splitting and adjusting:** only flow sections can be split or adjusted. Content crossing a cut
  needs an explicit `crossing=` choice.
- **Single reviewer:** there's no locking, so don't review the same run from two terminals at once.

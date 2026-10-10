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

## Three breakpoints

`capture URL -o DIR` (without `--width`/`--height`) loads the page three times, as each device would:

| Breakpoint | Viewport | Emulated device |
|---|---|---|
| laptop | 1440×900 | desktop browser |
| tablet | 768×1024 | iPad Mini (user agent, touch, mobile mode) |
| mobile | 390×844 | iPhone 13 (user agent, touch, mobile mode) |

Each load is a normal run in `DIR/laptop`, `DIR/tablet`, `DIR/mobile`, so `review`, `overlay`, `export` and
`evaluate` work on each. `detect DIR` and `report DIR` handle all three, and `report DIR` writes the merged
`DIR/report.json`:

- Sections follow the laptop layout, plus sections that exist only at smaller sizes, in their place.
- Each section has `position` per breakpoint and `hidden`: the breakpoints where none of its content shows.
- Each content entry has `hidden` when it is missing at some breakpoints (omitted when shown everywhere).

**Matching content across loads:**

1. **Same tag and text:** the same content, wherever it sits. This covers a desktop nav link and the
   same link in the mobile menu, or an image in a gallery that reshuffles and re-rows itself per width.
2. **Same DOM slot** (a path such as `#work>ul>li[3]>h3`): the same content, even with different text.
   This is what keeps random content (a rotating testimonial) from being listed once per breakpoint.
3. **Otherwise:** the content exists only at those breakpoints.

**Limitations:**

- Random content that also changes the page's structure between loads can still be mismatched.
- Element `id`s generated anew on each load make DOM paths differ; matching then falls back to text.

## Section screenshots and the board

- **Section screenshots:** `report` also saves one screenshot per section in `RUN/sections/`
  (`NN-<id or role>.png`, full resolution). For a three-breakpoint capture, each breakpoint folder gets
  its own.
  - **How they are taken:** `capture` saves an offline copy of the page (`page.mhtml`: Chrome's snapshot of
    the DOM with styles, images and fonts, and no scripts). Chrome's snapshot only links web fonts, and the
    copy loads nothing from outside itself, so `capture` first writes the page's web fonts into it (fonts
    from same-origin stylesheets; a cross-origin stylesheet such as a font CDN's cannot be read). Each section is then screenshotted from that
    copy with **the other sections and floating elements painted in front of it made transparent**, so
    cookie banners, sticky bars or a neighbouring section pulled up over it do not cover it. What is
    painted behind it stays, e.g. a background its text is designed to sit on. Paint order follows the
    browser's rule: higher `z-index`, then positioned over static, then later in the page. Nothing
    reflows, and the site is not visited again.
  - **Framing:** each section is framed where its own elements are in the copy. Styles that scripts added
    can be missing from the copy, so its layout may differ from the capture. When the copy sizes a
    section more than 10% wider, narrower, taller or shorter than captured (text re-flowed one word per
    line, say), the section is cropped from the live capture's screenshot instead.
  - **Sticky sections:** a section with sticky elements (content pinned while scrolling, such as reveal
    footers) at most two screens tall is shot as seen at the end of scrolling through it, so the pinned
    content sits over what scrolls under it (that shot is one screen tall). A longer one, such as a
    pinned "Women" label beside a column of products, is shot whole, laid out flat.
  - **Canvas, video and iframe sections:** the copy does not keep canvas drawings, video frames or
    iframe contents, so sections holding them are cropped from the live capture's screenshot. Anything
    that covered them at capture time stays in their picture.
  - **Older captures:** captures without the copy fall back to cropping `screenshot.png`.
- **The board:** `section-scan board` writes `boards/sections.excalidraw`, one Excalidraw file for every
  page.
  - **Columns:** pages left to right, each shown three times side by side (laptop, tablet, mobile), every
    column built from that breakpoint's own screenshot. The note at each column's top left has the title,
    link, industry and scope once classified, and the breakpoint. A single-viewport capture has one
    column.
  - **Sections:** top to bottom in each column, each with a note at its top left (its confirmed or
    proposed label, or its role). A section hidden at a breakpoint is absent from that column.
  - **Which captures:** without arguments it takes the most recent capture of each page in `./runs`;
    pass folders to choose.

**Re-running a site:** `capture` into an existing folder updates it. The new capture replaces the old
screenshot, elements and everything derived from them. `classification.json` and `review.json` are kept:
classification reuses only proposals whose input is unchanged, and a review asks for `--restart` once
the proposals change.

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
- **Images:** every image is an entry in page order: `{ "tag": "img", "text": "<alt>", "src": "<url>" }`.
  - `text` is absent when the image has no alt text (or `alt=""`).
  - `src` is the file the browser displayed, resolved to a full URL. Embedded `data:` images keep only
    their type (e.g. `data:image/png`).
  - Across breakpoints, an image without alt text is matched by its `src`.
  - Jev's classification input keeps alt text only; images with nothing but a URL are left out of it.
- **What text counts:** the DOM text, including parts not painted at capture (a closed `<details>`,
  content waiting for an animation). Left out: elements removed with `display: none` (typically duplicate
  mobile menus) and screen-reader-only text.
- **Older captures:** captures made before extractor 3 have no `id`, title or description, and only
  80-character text previews.

## Classifying sections (Jev)

```bash
uv run section-scan classify runs/example            # propose with Jev, then confirm each label
uv run section-scan classify runs/example --no-review
uv run section-scan classify runs/example --hide-dom-id   # evaluation: same content without DOM ids
```

- **API key:** put `TYPESAFE_API_KEY=…` in `./.env` (git-ignored; a real environment variable wins).
  `TYPESAFE_DEFAULT_MODEL` picks the model (default `jev-latest`).
- **Labels:** the registry in `src/section_scan/labels.py` (version `REGISTRY_VERSION`), shared by the
  Jev requests and the review screen: `header`, `footer`, `hero`, `about`, `services`, `portfolio`,
  `case_studies`, `testimonials`, `faq`, `gallery`, `contact`, `cta`, `other`, `unknown`.
- **Input per section:** the page title and description, order, layout role, DOM id (a hint), and
  `message` with its hierarchy. Exact duplicates are removed. Long content keeps the headings and the
  first items of each group, and is marked `input_shortened`.
- **Requests:** a section with no usable text is proposed `unknown` (`insufficient_content`, source
  `preprocessing`) without a request. Every other section gets one `Choice` request. A failed request is
  `failed`, never `unknown`, and is retried on the next run.
- **Review:** sections are shown in page order with the heading, a preview, the proposal and the next two
  labels. Actions: accept, choose another, leave unresolved, or quit (progress is kept). Your decision is
  stored separately from Jev's proposal.
- **Files:** `classification.json` holds the full state (proposals with probabilities, model, status and
  decisions), saved after every step. `report-classified.json` is the report with a compact
  `classification` per section and for the page: `{"label": …, "confidence": …}`.
  - The label is your confirmed one, else Jev's proposal; `null` when unresolved or failed.
  - The confidence is Jev's confidence in that label; `null` when you chose a different label or no
    request was made.
- **Reuse:** proposals are reused only when the input, instructions, registry and requested model all
  match. A decision made on older evidence is dropped.
- **Page: industry and scope.** After the sections, one request asks two `Choice` questions about the
  whole page:
  - **`industry`:** about 20 broad sectors plus `other`/`unknown`.
  - **`scope`:** the page type, e.g. `company_site`, `personal_portfolio`, `product_landing`,
    `service_landing`, `online_store`, `blog_article`, `documentation`.

  Its input is the domain, title, description, and each section's role, label (your confirmed label,
  else the proposal), headings and an excerpt. Both registries are in `labels.py`
  (`PAGE_REGISTRY_VERSION`). The page is reviewed like a section, and the export adds a top-level
  `classification` with `industry` and `scope`, each `{"label", "confidence"}`. If you quit the section review, the page waits for the
  next session, so it is judged on your final section labels.

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

## route-map: a site's route families

A second command in this repo. It turns one website into a compact map of **route families**: 1,000
product pages become one route, `/product/{segment_2}`, instead of 1,000 entries. Single pages such as
`/about` stay on their own. It infers routes from the URLs it sees. It does not read the site's source
router and does not prove that every value works. Its output is not an SEO XML sitemap.

The plan is `vault/AI-Memory/02_projects/quickshop/sitemap-route-generator-plan.md`. Milestone 1 (built)
maps a list of URLs offline. Discovery from the seed URL alone (robots.txt, sitemaps, links), validation
on sampled pages and page roles are the next milestones.

```bash
uv run route-map https://shop.test/ --inventory urls.txt -o route-map.json   # or: python -m route_map ...
```

- **Input:** `--inventory` is a text file with one URL per line (`#` lines are skipped). URLs on another
  host than the seed, or that are not HTTP(S), are listed under `crawl_report.rejected_urls`.
- **URL identity (conservative):** the scheme, default ports, ordinary `#anchors` and tracking parameters
  (`utm_*`, `gclid`, `fbclid`, ...) are ignored. Everything else is kept as written: path case, `%2F`,
  extensions, the trailing slash, and unknown query parameters with their order, blank values and repeated
  keys. Duplicates become `aliases` of one URL record.
- **Families:** URLs of the same shape (host, depth, trailing slash, extension, query keys) that differ in
  exactly one slot become a family once that slot takes at least 3 different values, e.g.
  `/blog/{segment_2}`, `/p/{segment_2}.html` or `/search?q={q}`. Rules that hold:
  - `/blog/{slug}` and `/blog/category/{slug}` are different families.
  - Locale prefixes stay separate (`/en/product/...`, `/fr/product/...`).
  - Root pages (`/about`, `/contact`, `/privacy`) are never folded into a catch-all `/{segment_1}`.
  - When a URL fits two candidates, the larger family claims it.
- **Status:** without page validation, every family is `provisional` (a structural hypothesis), and its
  members include possible exceptions such as `/product/compare` until sampled pages are compared. URLs in
  no family are `individual`. Each family carries a stable sample plan (`sample_ids`, spread over different
  value shapes) for validation.
- **Output:** versioned JSON (`schema_version`) holding `routes` (each with at most 3 `examples`), `urls`
  (the full inventory, each with its `route_id`) and `crawl_report` (budgets, rejected URLs, limitations
  such as hash-router URLs). Its IDs and ordering depend only on the set of URLs, not on their order.
- **Not yet:** one variable slot per family, so nested routes such as `/category/{a}/page/{b}` are not
  inferred. Parameters have neutral names (`segment_2`); role-based names such as `product_slug` come with
  page roles.

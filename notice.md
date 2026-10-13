# Notice: deferred items

Known issues and decisions postponed on purpose. Each entry says why it matters and when to revisit it.

## Capture can hang on a frozen page (deferred)

`page.evaluate` calls (layout signature, scroll pass, element extraction) have no timeout. If a page's own
scripts block the browser's main thread, `capture` never returns. Every other wait is bounded.
Revisit if a real capture ever stalls. Fix: run each evaluate under a deadline and record the capture as
failed with a clear reason.

## Very tall pages are untested (low priority)

Full-page screenshots of pages tens of thousands of pixels tall may be truncated or fail. The current use
case does not need very long pages. Revisit only if one is needed.

## Element IDs are capture-specific (next milestone)

Exported sections reference element IDs (`e42`) that are only valid for one capture. If per-section
wireframe extraction needs to match sections across recaptures or page versions, add stable references
(selectors or content fingerprints). Decide at the start of that milestone.

## One desktop viewport per capture (needed later)

Mobile layouts stack, hide and reorder content differently, so mobile sections need their own capture
(`--width 390 --height 844`) and their own review. The pipeline already works per viewport. Linking the
desktop and mobile versions of the same section is not built.

## Blank areas the DOM reports as visible (deferred)

Some scroll-driven effects move content off the painted page while the browser still reports it visible,
with readable text (lingowp.com's "Stop putting translation off" section: about 1,900 px of blank
screenshot). The hidden-content limitation only sees content captured with opacity 0 or
`visibility: hidden`, so this case is not flagged. Catching it needs a pixel check on the screenshot
(large uniform bands inside a section that the element data says hold content). Revisit if such pages
matter for review.

## Closed `<details>` content counts as visible (deferred)

Content of a closed `<details>` element (e.g. a language dropdown) is not painted and not hit-testable,
but is still reported visible. A rule "element absent from the browser's hit stack at its own centre"
would catch it on the first screen. Small and low impact; revisit with the next visibility pass.

## route-map: limits after milestone 2 (deferred)

- **Two parameters at most (resolved for two).** Sibling families now merge into
  `/collections/{category_slug}/products/{product_slug}`. Three or more varying segments are still not
  inferred; add a further merge pass if a site needs it.
- **No rendering.** Pages are read as served HTML. A JavaScript shell (a seed page without links) is
  only reported as a limitation; optional Playwright rendering is a later extension.
- **Action-link filter by URL words.** Links whose path or query holds words such as `logout`,
  `add-to-cart`, `remove` or `delete` are never requested, so a real page such as
  `/product/remove-tool` would be skipped too. A smarter rule would look at the link's context
  (a form button, a `rel` or a `data-` attribute).
- **Templates: rich-text rule is a heuristic.** A body counts as one `rich_text` token only when it is
  mostly paragraphs, headings, lists and figures. A large custom component inside a post body (a div
  pricing table, an embedded form) keeps its structure, so that post may become a template of its own.
  Revisit with real blogs and page builders.
- **Templates: saturation is not proof.** A family stops being sampled once 5 checked pages (counted per
  round) touch no template it had not touched before, so a layout used by 1 page in 500 can be missed. `stop_reasons` shows the families the
  budget left open, not the ones that saturated too early.
- **Templates: served HTML only.** Two layouts that differ only in CSS or in client-side rendering count
  as one template. Comes with optional rendering.
- **Templates: stop-reason gaps.** A family first found during the templates phase (from the links of a
  sampled page) and never checked gets no stop reason when the budget runs out: the reasons name only
  families with at least one checked member, so such a family is missing from `stop_reasons` silently.
- **Templates: 0.5 threshold tuned on fixtures.** The same Jaccard threshold groups pages into templates;
  a false merge hides a layout from the screenshots. Re-measure on real sites before trusting it.

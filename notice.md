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

# Evaluation

Baseline results, recorded 9 October 2026 (detector 2, default `detector.toml`: threshold 3.0, fallback
floor 16 px, at least 2 spacing observations per side, spacing measured two stacking levels deep, with
single-row wrappers passed through). Detector 1 counted every container as a level; see "What changed in
detector 2" below.

## How to reproduce

```bash
uv run section-scan capture "file://$PWD/fixtures/candidates-landmarks.html" -o runs/eval-landmarks --width 1000 --height 700
uv run section-scan evaluate runs/eval-landmarks evaluation/annotations/candidates-landmarks.json
```

`evaluate` runs the combined detector and
two baselines in memory on the same capture. It never changes `proposals.json` or a review.

| Mode | Decides with |
|---|---|
| combined | the plan's five rules: landmarks, strong gap plus independent support, page-wide paint or landmark transitions, repeated/tight = keep, conflicts = ambiguous |
| container | structure and paint only: page-wide background changes, landmark or boxed-sibling transitions |
| gaps | the gap ratio only (header/footer landmarks still split in every mode) |

Annotations (`annotations/*.json`) list the expected sections top to bottom, each anchored by texts inside
it. A **missed** boundary means two neighbouring expected sections share one proposed section. A **false
split** means one expected section's anchors land in different proposed sections. **Est. edits** adds the
ambiguous sections a reviewer must inspect.

## Results

| Capture | Mode | Expected | Proposed | Missed | False splits | Extra | Ambiguous | Est. edits |
|---|---|---|---|---|---|---|---|---|
| lingowp.com | combined | 8 | 9 | 0 | 0 | 1 | 1 | 1 |
| | container | 8 | 9 | 0 | 0 | 1 | 0 | 0 |
| | gaps | 8 | 9 | 0 | 0 | 1 | 1 | 1 |
| playwright.dev/python | combined | 9 | 9 | 0 | 0 | 0 | 0 | 0 |
| | container | 9 | 9 | 0 | 0 | 0 | 0 | 0 |
| | gaps | 9 | 9 | 0 | 0 | 0 | 2 | 2 |
| candidates-landmarks fixture | combined | 9 | 9 | 0 | 0 | 0 | 4 | 4 |
| | container | 9 | 8 | 1 | 0 | 0 | 2 | 3 |
| | gaps | 9 | 8 | 1 | 0 | 0 | 6 | 7 |
| candidates-divs fixture | combined | 9 | 9 | 0 | 0 | 0 | 4 | 4 |
| | container | 9 | 8 | 1 | 0 | 0 | 2 | 3 |
| | gaps | 9 | 4 | 5 | 0 | 0 | 3 | 8 |
| normalize-base / -wrapped fixtures | combined | 4 | 4 | 0 | 0 | 0 | 0 | 0 |
| | container | 4 | 4 | 0 | 0 | 0 | 0 | 0 |
| | gaps | 4 | 4 | 0 | 0 | 0 | 2 | 2 |

**Extra** counts proposed sections that hold no anchor. On lingowp it is the dashboard band (y 1882–3028):
a correct section that has no text, so the annotation cannot anchor it. Extra sections are listed by
`evaluate` for a person to judge; they are not automatically wrong.

## What the baseline shows

- **No anchored false splits in any mode.** No expected section's anchors were cut apart. Splits that
  only separate textless content are not measured by anchors; they show up as "extra" sections instead.
- **Structure carries the two real pages.** Both use `<section>` or page-wide colour bands, so
  container-only evidence is as good as the combined detector there.
- **Gap evidence helps where structure is silent.** On the landmark fixture, the combined detector finds
  the "Plain → Article" boundary that container-only misses (two transparent, unboxed regions).
- **The gap ratio is now known on real pages.** Level-0 flow-to-flow boundaries with a ratio: lingowp
  6 of 6, playwright 5 of 6 (detector 1: 1 of 6 each). Gaps-only became nearly as good as the other
  modes there: lingowp went from 1 missed and 6 ambiguous to 0 missed and 1 ambiguous, playwright from 6
  ambiguous to 2. The combined detector's results are unchanged on all six captures, because structure
  already decided those boundaries.
- **The fixtures stay unmeasurable, correctly.** Their sections hold one row of text each, so there is no
  internal gap at any depth; the ratio is still omitted and the gaps-only rows did not change.

### What changed in detector 2

Detector 1 counted every container as a spacing level, so theme wrappers (section → container → row of
columns) used up the two levels before any rows were reached, and most sides had 0 or 1 gaps. Detector 2
counts only stacks of rows as levels: a container whose children form one row (a wrapper, or columns
side by side) is passed through. The estimate stays local (the region's own groups), vertical, and
never includes the candidate gap.

A side with a single gap (a heading and one paragraph) is still unknown. Accepting one observation was
measured and rejected: on lingowp a lone 64 px heading gap set the FAQ section's scale, which hid the
FAQ → call-to-action boundary from gaps-only (1 missed, 1 false split). Setting
`min_spacing_observations = 1` reproduces that behaviour if wanted.

## Observed failure cases

1. **Sections nested two levels down** (divs fixture, Part A/B): fixed. Inner alternatives are now explored
   recursively, up to `MAX_NESTING` levels (candidates.py), and the detector promotes them level by level.
2. **Scroll-driven content** (lingowp "Stop putting translation off"): later steps are invisible in a
   static capture. The section is kept whole and flagged ambiguous; the empty area is not detected as a
   limitation.
3. **Textless sections cannot be annotated** with text anchors (lingowp dashboard band). Boundary error
   in pixels is not measured yet; it needs y-coordinate annotations.
4. **Ambiguity is high on fixtures** (4 of 9 on the landmark page): unknown ratios (single-row
   sections) plus spanning content, by design, but it costs reviewer time.

## Before tuning

- **Too few real pages.** Two real pages is a development set, not evidence of generalization. Collect
  varied real captures (different themes and builders) and keep a held-out set untouched until the
  threshold, `min_spacing_observations` and the spacing depth have been tuned on the rest.
- **Annotator.** The real-page annotations were made by Claude from the screenshots; a person should
  verify them.
- **Acceptance targets.** Choose them from these recorded baselines, not in advance, as the plan says.

# Evaluation

Baseline results for the first version, recorded 9 October 2026 (detector 1, default `detector.toml`:
threshold 3.0, fallback floor 16 px, at least 2 spacing observations per side, spacing measured two
container levels deep).

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

| Capture | Mode | Expected | Proposed | Missed | False splits | Ambiguous | Est. edits |
|---|---|---|---|---|---|---|---|
| lingowp.com | combined | 8 | 9 | 0 | 0 | 1 | 1 |
| | container | 8 | 9 | 0 | 0 | 0 | 0 |
| | gaps | 8 | 8 | 1 | 0 | 6 | 7 |
| playwright.dev/python | combined | 9 | 9 | 0 | 0 | 0 | 0 |
| | container | 9 | 9 | 0 | 0 | 0 | 0 |
| | gaps | 9 | 9 | 0 | 0 | 6 | 6 |
| candidates-landmarks fixture | combined | 9 | 9 | 0 | 0 | 4 | 4 |
| | container | 9 | 8 | 1 | 0 | 2 | 3 |
| | gaps | 9 | 8 | 1 | 0 | 6 | 7 |
| candidates-divs fixture | combined | 9 | 8 | 1 | 0 | 4 | 5 |
| | container | 9 | 7 | 2 | 0 | 2 | 4 |
| | gaps | 9 | 4 | 5 | 0 | 3 | 8 |
| normalize-base / -wrapped fixtures | combined | 4 | 4 | 0 | 0 | 0 | 0 |
| | container | 4 | 4 | 0 | 0 | 0 | 0 |
| | gaps | 4 | 4 | 0 | 0 | 2 | 2 |

lingowp proposes 9 sections for 8 expected: its dashboard band has no text, so the annotation cannot
anchor it and it is not counted.

## What the baseline shows

- **No false splits in any mode.** The detectors err towards keeping regions together or leaving them
  ambiguous, never towards cutting an expected section.
- **Structure carries the two real pages.** Both use `<section>` or page-wide colour bands, so
  container-only evidence is as good as the combined detector there.
- **Gap evidence helps where structure is silent.** On the landmark fixture, the combined detector finds
  the "Plain → Article" boundary that container-only misses (two transparent, unboxed regions).
- **The gap ratio is often unknown.** Spacing is measured only in compact local groups, as the plan asks,
  so many sides have fewer than 2 internal gaps. The ratio is then omitted, which makes gaps-only
  ambiguous on most boundaries.

## Observed failure cases

1. **Sections nested two levels down** (divs fixture, Part A/B): candidate generation keeps only one level
   of inner alternatives, so a split inside an inner region is not offered.
2. **Scroll-driven content** (lingowp "Stop putting translation off"): later steps are invisible in a
   static capture. The section is kept whole and flagged ambiguous; the empty area is not detected as a
   limitation.
3. **Textless sections cannot be annotated** with text anchors (lingowp dashboard band). Boundary error
   in pixels is not measured yet; it needs y-coordinate annotations.
4. **Ambiguity is high on fixtures** (4 of 9 on the landmark page): unknown ratios plus spanning
   content, by design, but it costs reviewer time.

## Before tuning

- **Too few real pages.** Two real pages is a development set, not evidence of generalization. Collect
  varied real captures (different themes and builders) and keep a held-out set untouched until the
  threshold, `min_spacing_observations` and the spacing depth have been tuned on the rest.
- **Annotator.** The real-page annotations were made by Claude from the screenshots; a person should
  verify them.
- **Acceptance targets.** Choose them from these recorded baselines, not in advance, as the plan says.

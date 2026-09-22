# Proposal: fix segmentino label loss + cap consecutive-merge collapse

## Why

Diagnosed on a real song in the user's live library (`e0f7a3435bd3296c`,
"It's the Most Wonderful Time of the Year" — Andy Williams, 152.6s):
detected as **3 sections**, one of which ("Verse") spans **0–126.2s, 83%
of the entire song**. Traced end-to-end through the real hierarchy/story
JSON on disk (not a synthetic repro):

1. The raw boundary detector found 8 reasonable, evenly-spaced boundaries
   (0.2s, 9.6s, 49s, 70s, 77s, 115s, 126s, 140s) — correct.
2. Every one of those 8 marks has `label: null` in the hierarchy JSON.
   `algorithms_run` confirms `segmentino` (the plugin that's supposed to
   supply structural group labels — A/B/C…) did run.
3. **Root cause, confirmed by code comparison, not guesswork:**
   `src/analyzer/algorithms/vamp_segmentation.py`'s `SegmentinoAlgorithm._run()`
   calls `vamp.collect(audio, sample_rate, self.plugin_key,
   parameters=self.parameters)` with **no `output=` argument**. Every
   other structural/multi-output Vamp wrapper in this codebase explicitly
   selects an output — `QMSegmenterAlgorithm` (the sibling structural
   segmenter, `vamp_structure.py:23`) sets `vamp_output = "segmentation"`
   and passes `output=self.vamp_output`; so do `vamp_beats.py`,
   `vamp_onsets.py`, `vamp_pitch.py`, `vamp_harmony.py` (chordino, which
   *does* get labels reliably). Segmentino is the sole outlier relying on
   Vamp's default-output behavior, which for a multi-output plugin is not
   guaranteed to be the labelled one.
4. With every raw mark unlabeled, `src/story/builder.py` cannot route
   through the label-aware classifier (`section_classifier.py`'s
   `_classify_by_labels()`) — it silently falls back to the weaker
   energy-percentile-only heuristic (top ~25% energy → chorus, rest →
   verse) for *every* song where this fires, not just this one.
5. On this song (uniform-dynamics orchestral Christmas standard), 6 of
   the 8 segments landed below the chorus threshold, all got labeled
   `verse`, and `builder.py`'s consecutive-same-role merge (Step 4b,
   which exists specifically to collapse segmentino's normal
   over-splitting) glued all 6 into one 126-second block. This is the
   exact failure pattern `docs/segment-classification-changelog.md`
   already documents for a different song (Ghostbusters, 2026-03-31,
   "93-second s09 'verse'... swallowed 6 sections") — reproducing live,
   on a different song, seven months later, because the upstream label
   loss silently pushes *every* affected song down the same weak path.

**One piece I cannot confirm from this machine**: whether the correct
`output=` value for `segmentino:segmentino` is `"segmentation"` (matching
QM's convention) or a different output ID — that needs
`vamp.list_outputs_of("segmentino:segmentino")` run once against the real
plugin in the Linux dev container (`vamp` has no native host library on
Windows). Flagged as a required verification step before this ships, not
guessed at.

## What changes

1. **`src/analyzer/algorithms/vamp_segmentation.py`**: add explicit
   `vamp_output` class attribute + `output=self.vamp_output` on the
   `vamp.collect()` call, matching every sibling Vamp wrapper's
   convention. Exact output ID confirmed against the real plugin first
   (see Verification below) — code written against whichever ID that
   turns out to be.
2. **Diagnostic, regardless of the fix landing correctly on the first
   try**: log a warning (existing `warnings` mechanism already surfaced
   to the UI) when segmentino runs but returns zero labeled marks, so a
   future regression here is visible in the analysis output instead of
   silently degrading to the energy-only fallback the way this one did
   for an unknown length of time.
3. **`src/story/builder.py` Step 4b (consecutive-same-role merge)**:
   defense-in-depth cap, independent of the segmentino fix — a single
   merged run should not be allowed to swallow more than a fixed fraction
   of the song (proposed: 50%, tunable) even when every input section
   shares the same role. Regardless of *why* 6 segments all got labeled
   "verse", a single section covering 83% of a song is never a
   reasonable structural output and the merge step should refuse to
   produce one — same spirit as the existing `_genius_quality_ok()` gate
   ("reject if any single section covers >60% of song duration") but
   applied at the point the collapse actually happens, not after the
   fact on a since-removed data source.

## Verification (must happen before merge, not assumed)

Run in the real dev container (this machine has no `vamp` host library):
```python
import vamp
print(vamp.list_outputs_of("segmentino:segmentino"))
```
Confirms the real output ID and that it carries labels — re-run the
existing analysis on this exact song afterward and confirm
`hierarchy["sections"]` entries carry non-null labels, then confirm the
Theme screen shows more than 3 sections for it.

## Impact

- Affected code: `src/analyzer/algorithms/vamp_segmentation.py`,
  `src/story/builder.py` (Step 4b only), `docs/segment-classification-changelog.md`
  (mandatory entry per `CLAUDE.md`'s rule for any section-detection/merge/
  classification change).
- Every song analyzed since segmentino label loss began is affected by
  item 4 above (silent fallback to the weaker heuristic) — re-analysis
  will change their detected sections, not just this one song's. That's
  the intended fix, not a regression, but worth flagging since story
  JSON caching means old results won't self-correct without a fresh
  `force=True` analysis (or a schema-version bump forcing one, matching
  this repo's established pattern for exactly this situation — see the
  2026-07-25 changelog entry).
- No change to `boundary_cluster.py`, `section_classifier.py`'s
  thresholds, or `boundary_refinement.py`'s three fixes — those are
  correctly scoped as separate follow-ups (energy-threshold tuning,
  agreement-score-driven merging) once this label-loss bug stops
  masking whether they're actually the next weakest link.

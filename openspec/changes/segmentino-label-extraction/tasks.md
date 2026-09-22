## 1. Verify the real output ID (blocking — do first)

- [ ] 1.1 In the Linux dev container: `python -c "import vamp; print(vamp.list_outputs_of('segmentino:segmentino'))"`. Confirm which output ID carries structural group labels.
- [ ] 1.2 If it's not `"segmentation"`, update the design/implementation to the real value before proceeding — do not ship the `"segmentation"` guess unverified.

## 2. Fix the extraction

- [ ] 2.1 `src/analyzer/algorithms/vamp_segmentation.py`: add `vamp_output = <confirmed id>` class attribute; pass `output=self.vamp_output` to `vamp.collect()`.
- [ ] 2.2 Re-run analysis on song `e0f7a3435bd3296c` ("It's the Most Wonderful Time of the Year") with `force=True`. Confirm `hierarchy["sections"]` entries now carry non-null `label` values, and the Theme screen shows more than 3 sections with no single section covering anywhere near 83% of the song.

## 3. Add the label-loss warning

- [ ] 3.1 In `src/analyzer/orchestrator.py`'s L1 structure stage (~line 485-501), after building `sections`, detect "segmentino ran (`algorithms_run` contains `segmentino`) but contributed zero labelled marks" and append a `result.warnings` entry.
- [ ] 3.2 Confirm the warning surfaces through the existing UI warnings display (already wired for other warnings — verify, don't assume).

## 4. Cap the consecutive-merge collapse

- [ ] 4.1 `src/story/builder.py` Step 4b: before extending a merged run, check whether the resulting `(end_ms - start_ms)` would exceed 50% of `duration_ms`; if so, keep the sections separate.
- [ ] 4.2 New test fixture: deliberately uniform-energy synthetic audio (extend `tests/conftest.py`'s existing fixture-generation pattern) that would otherwise classify every segment as the same role — assert no merged section exceeds the cap.
- [ ] 4.3 Run existing `tests/` for `section_classifier.py`/`section_merger.py`/`builder.py` — confirm no regression on songs that legitimately have long same-role runs under 50%.

## 5. Schema version + docs

- [ ] 5.1 Bump `SCHEMA_VERSION` (hierarchy) per the 2026-07-25 changelog's documented lesson — otherwise `fresh=False` reanalysis keeps silently re-serving cached pre-fix results.
- [ ] 5.2 Append an entry to `docs/segment-classification-changelog.md` (mandatory, append-only) documenting this bug, the evidence trail, and the fix — matching the existing entries' format.
- [ ] 5.3 Update readers that hard-check schema version if any remain (per the 2026-07-16 crash-stem entry's lesson about brittle `== "2.0.0"` checks) — verify via `src/schema_check.py`.

## 6. Verify against the real library

- [ ] 6.1 Re-analyze all 3 songs currently in the user's library (`force=True`) and manually compare before/after section counts and boundaries.
- [ ] 6.2 Confirm "Magic Mirror" and the Kacey Musgraves song (the other two real library entries) either improve or are unaffected (already had non-degenerate structure).

## 7. Acceptance gate

- [ ] 7.1 Run `xlight-evaluate gate --quick` in the real dev container.
- [ ] 7.2 Run `xlight-evaluate snapshot-analyzer` + review the baseline diff — confirm changes are confined to section boundaries/labels/counts, not unrelated fields.

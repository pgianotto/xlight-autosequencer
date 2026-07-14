# STATUS — xlight-autosequencer

> Single source of truth for resuming work. Read this FIRST when starting a session.
> Update this file at the end of every work phase so the next `/clear` resumes in 1 read.
> Last updated: 2026-07-14

---

## ✅ Done

- Arch prop-family recipe: chorus alternates Single Strand/Shockwave, bridge alternates to Spirals, chase direction + chase-size (Color_Mix1) rotate per section occurrence — merged to `main`.
- Same direction/size rotation extended to cane/horizontal/vertical (minitree deliberately excluded — its data stays fixed to Right-Left).
- Fixed stale `video_path` on re-import in `src/review/api/v1/import_video.py` (always adopts the latest drop, not just the first).
- Added `Shader` effect (scoped to Plasma Emitter.fs) + 3 variants, and a new energy-gated whole-house composite mechanism (`AccentPolicy.whole_house_layers`, `_place_whole_house_composite` in `effect_placer.py`) that stacks extra layers on `01_BASE_All`, mined from the corpus's "All" group idiom.
- Installed the real `openwolf` CLI (`npm install -g openwolf`), ran `openwolf init` — it auto-wired unrequested Codex integration (removed) and silently deleted the Branch Discipline / Code Review Discipline sections from OPENWOLF.md plus inserted an unsolicited "Astryx" framework recommendation into reframe-frameworks.md (both restored/removed). `.claude/settings.json` hook registration now real. Note: live hooks auto-write noisy "auto-detected" entries to `.wolf/buglog.json`/`anatomy.md` on every Read/Edit — currently just `git checkout`-ing those away each time; worth revisiting whether to disable that specific hook behavior.
- **Recovered all genuinely-missing work from branches ≤2 days old** (per user's cutoff): cherry-picked icicle recipe, mega-topper recipe + topper→hero promotion, star recipe, corpus-paired-hero pairing + always-fade-out, matrix motion rotation (4 looks), and the `mine_arch_corpus.py` tool script from `fix/spirals-textctrl-movement` / `feat/arch-sequencing-corpus-miner`. Deliberately dropped: the old All-group recipe (superseded by today's whole-house composite — removed `all_group`/`_SHOCKWAVE_ALL`/`_PINWHEEL_ALL`/`burst_volley` + related dead code), and several already-redundant commits (textctrl migrations, a devcontainer doc already present differently). Deleted all now-fully-accounted-for local branches (remotes untouched as backup).
- Full test suite green throughout (2916 passed at last full run).

---

## 🚀 Next phase

**Goal:** none queued — this was a cleanup/consolidation session. Next work starts fresh from whatever the user brings.

### Open decisions
- Whether to re-mine/redo the All-group idiom later as an enhancement on top of today's whole-house composite (volley pacing, color-cycling backbone) — deferred, not decided.
- Whether to investigate/disable the openwolf hook that auto-generates noisy "auto-detected" buglog.json entries and drastically rewrites anatomy.md on every Read/Edit.

---

## 📁 Active architecture

- **Stack:** Python 3.11+ (analyzer/generator/CLI), Flask + React/Vite review UI, click CLI. See CLAUDE.md for the full stack list.
- **Key modules:** `src/generator/corpus_recipes.py` (mined prop-family idioms) + `src/generator/effect_placer.py` (placement engine, `_place_corpus_recipe`, `_place_whole_house_composite`) + `src/generator/plan.py` (orchestrates `build_plan`, computes `AccentPolicy` gates once per section).
- **Patterns:** corpus-mined presets always cite the actual mined stat in a comment (see `_SHOCKWAVE_BURST`, `_SPIRALS_ARCH_BRIDGE`, etc.); accent passes (`drum_hits`/`impact`/`whole_house_layers`) run as a second pass in `build_plan` AFTER `place_effects`, not inside it — any isolation test re-running `place_effects` alone must disable all of them via `GenerationConfig`.

---

## ⚠️ External blockers (don't block coding)

- The devcontainer's `xlight-review` server does NOT hot-reload backend Python changes — must be killed/restarted after every commit (see CLAUDE.md → "Restarting the dev review server after a commit"). Always check the UI's `api <commit>` version banner before concluding a fix "didn't work."
- Mined corpus extracts (`docs/*_sequencing_corpus/`) must never be pushed to GitHub (purchased reference sequences) — gitignored on `main`.

---

## 🔧 Useful commands

```bash
# Restart the devcontainer review server (Git Bash needs MSYS_NO_PATHCONV=1)
MSYS_NO_PATHCONV=1 docker exec xlight-dev pkill -f xlight-review
MSYS_NO_PATHCONV=1 docker exec -d xlight-dev /usr/bin/python3 /home/node/.local/bin/xlight-review --dev --host 0.0.0.0 --port 5000

# Run the generator/effects test suites
python -m pytest tests/unit/test_generator/ tests/unit/test_effects_library.py tests/unit/test_variant_library.py -q

# Full suite (excludes vamp/madmom-only suites not installed on this host)
python -m pytest tests/ -q --ignore=tests/microscope
```

---

## 📚 References (read IF needed)

- `.wolf/cerebrum.md` — User Preferences + Do-Not-Repeat + Decision Log
- `.wolf/anatomy.md` — token-efficient file index
- `.wolf/buglog.json` — known bugs + fixes

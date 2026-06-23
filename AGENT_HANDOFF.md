# NeutArr Upgrade Fix Handoff

## Goal

Fix NeutArr upgrade behavior for Radarr and Sonarr while keeping the changes reviewable as small, separate PRs.

The user does not want one large opinionated PR. Split behavior changes so the maintainer can accept or reject them independently.

## User Decisions

- Quality tier must outrank custom format score.
- A strictly better quality tier is always a valid upgrade even if the custom format score is lower.
- If quality tier is equal, upgrades should only consider strictly better releases.
- Sonarr season-pack upgrades should remain an option.
- Sonarr season-pack upgrades should require both:
  - a minimum count of cutoff-unmet episodes
  - a minimum percentage of the season cutoff-unmet
- Starting defaults requested by the user:
  - minimum cutoff-unmet episodes: `3`
  - minimum cutoff-unmet percentage: `40`
- Those Sonarr thresholds must be configurable in NeutArr settings and exposed in the web UI.
- Keep existing defaults/modes where possible. Avoid broad UX or workflow changes outside the specific bug fixes.

## What Was Verified

### 1. Current local NeutArr behavior

- Local clone path: `/home/bradley/code/NeutArr`
- GitHub auth is available via `gh`:
  - account: `bwsinger`
  - protocol: `ssh`
- Upstream repo: `I-am-PUID-0/NeutArr`

### 2. Radarr bug is real

Current Radarr upgrade eligibility code is in:

- `src/primary/apps/radarr/api.py`
- `src/primary/apps/radarr/upgrade.py`

Current logic in `get_cutoff_unmet_movies()`:

- fetch all movies
- fetch quality profiles
- compare current quality rank vs profile cutoff rank
- ignore current custom format score
- ignore `minUpgradeFormatScore`
- ignore `cutoffFormatScore`
- ignore whether any candidate release would actually beat the imported file

Then `process_cutoff_upgrades()` randomly picks eligible movies and triggers `MoviesSearch`.

This is too weak. It can send Radarr into a search for a movie that is only "cutoff unmet" in a broad sense, while the actual release Radarr grabs may not be strictly better than the current import under the user's intended rules.

### 3. Concrete live-server evidence for Radarr

Using the user's live Radarr:

- Quality profile `9` (`4k - Trash Guide`) has:
  - `upgradeAllowed: true`
  - `cutoff: 31`
  - `minUpgradeFormatScore: 1`
  - `cutoffFormatScore: 10000`

That confirms custom format score is part of the real upgrade policy and NeutArr currently ignores it.

Concrete title verified:

- `Army of Darkness` (`movieId=44`)
- Current imported file:
  - quality: `Bluray-2160p`
  - scene: `Army of Darkness 1992 Theatrical Cut UHD 4K BluRay 2160p HDR10 DTS-HD MA 5.1 H.265-MgB`
  - `qualityCutoffNotMet: true`
- Radarr history shows:
  - existing imported grab at `customFormatScore=3400`
  - later `UserInvokedSearch` grabs for `Remux-2160p` at `customFormatScore=1900`

This confirms NeutArr/Huntarr has already triggered searches that produce lower-scored releases than the existing file.

Important nuance:

- Under the user's chosen rule, the lower-scored `Remux-2160p` still counts as better than `Bluray-2160p` because quality tier wins.
- So not every "lower CF score" grab is a bug by itself.
- The actual bug is that NeutArr is not evaluating candidate releases against the current imported file using the intended ordering rules before grabbing.

### 4. Radarr release preflight endpoint exists

This was verified against the user's live Radarr:

- `GET /api/v3/release?movieId=44`

The payload includes enough preflight data to rank candidates before grabbing:

- `quality`
- `customFormats`
- `customFormatScore`
- `approved`
- `rejected`
- `rejections`
- `downloadAllowed`
- `infoUrl`
- `guid`
- `indexer`

This is the key finding that makes a proper fix possible without relying on `MoviesSearch`.

### 5. Sonarr bug is real

Current Sonarr upgrade code is in:

- `src/primary/apps/sonarr/upgrade.py`
- `src/primary/apps/sonarr/api.py`

Current season-pack upgrade mode:

- samples episodes from `wanted/cutoff`
- groups them by series+season
- randomly chooses seasons
- triggers `SeasonSearch` for the full season

There is no threshold for:

- minimum number of cutoff-unmet episodes in the season
- minimum percentage of the season cutoff-unmet

So one or two low-quality episodes can trigger a full season search.

### 6. Concrete live-server evidence for Sonarr

User's Sonarr config currently uses:

- `upgrade_mode: "seasons_packs"`

The NeutArr logs show repeated season searches triggered from tiny cutoff-unmet counts, for example:

- `Letterkenny - Season 2`
- `Shrinking - Season 2`
- `Smartypants - Season 1`

Those season searches were repeatedly triggered with only `1` or `2` cutoff-unmet episodes.

## Likely Implementation Areas

### Radarr backend

- `src/primary/apps/radarr/api.py`
- `src/primary/apps/radarr/upgrade.py`

Likely work:

- add a release-preflight function using `/api/v3/release?movieId=<id>`
- define a comparator for:
  - current imported file
  - candidate release
- ordering rules should be:
  - higher quality tier wins
  - if same quality tier, require strictly better candidate
  - custom format score should be used within the same quality tier
- only grab an explicit candidate if it beats the current file
- avoid `MoviesSearch` if explicit release grab is possible

Open technical question for implementation:

- whether NeutArr should switch from `MoviesSearch` to explicit release download for Radarr upgrades

Given the bug, explicit release download after preflight is likely the correct fix.

### Sonarr backend

- `src/primary/apps/sonarr/upgrade.py`
- possibly `src/primary/apps/sonarr/api.py` if more season metadata is needed
- `src/primary/default_configs/sonarr.json`

Likely work:

- add two new Sonarr settings:
  - `season_upgrade_min_cutoff_unmet_episodes`
  - `season_upgrade_min_cutoff_unmet_percent`
- only apply them in `upgrade_mode == "seasons_packs"`
- filter `available_seasons` before random selection
- season is eligible only if both thresholds pass

### Frontend / settings UI

Settings UI lives in:

- `frontend/static/js/settings_forms.js`
- `frontend/static/js/new-main.js`

The generated settings forms for Sonarr and Radarr are built there.

Likely UI work:

- add the two Sonarr threshold fields to the Sonarr settings form
- parse/save those fields in `getFormSettings`
- ensure defaults are present in `src/primary/default_configs/sonarr.json`

## Suggested PR Strategy

Do not send one PR with all behavior changes mixed together.

### PR 1: Sonarr season-pack threshold settings

Scope:

- add new Sonarr config keys
- add Sonarr UI fields
- persist/load them cleanly
- no behavior change yet, or behavior change only if values are present

Why:

- easiest PR to review
- low risk
- maintainer can accept the configurability independently of the logic

Suggested branch:

- `feature/sonarr-season-upgrade-threshold-settings`

### PR 2: Sonarr season-pack threshold enforcement

Scope:

- use the new threshold settings in `src/primary/apps/sonarr/upgrade.py`
- only affect `upgrade_mode == seasons_packs`
- no changes to episode upgrade mode

Why:

- keeps the Sonarr behavior change isolated
- easy to explain with logs and before/after examples

Suggested branch:

- `feature/sonarr-season-upgrade-threshold-enforcement`

### PR 3: Radarr stricter upgrade gating

Scope:

- replace quality-cutoff-only selection with current-vs-candidate comparison
- use release preflight data from `/api/v3/release?movieId=<id>`
- ensure only strictly better releases are grabbed
- keep the comparison rule aligned with the user's requested ordering:
  - quality tier first
  - custom format score second when quality tier ties

Why:

- this is the highest-risk and most behaviorally opinionated change
- it deserves its own PR with focused discussion

Suggested branch:

- `feature/radarr-strict-upgrade-preflight`

### Optional PR 4: Sonarr stricter candidate preflight

Only do this if needed after studying Sonarr release endpoints and whether `SeasonSearch` can still grab non-strictly-better releases.

This would be more invasive than the season threshold fix and should stay separate.

Suggested branch:

- `spike/sonarr-strict-upgrade-preflight`

## GitHub Workflow Strategy

1. Fork upstream with GitHub CLI:
   - `gh repo fork I-am-PUID-0/NeutArr --clone=false --remote=true`
2. Keep `upstream` pointing at the original repo.
3. Create one branch per PR from the same clean upstream base.
4. Keep commits small and topic-specific.
5. Open each PR separately with a narrow title and narrow rationale.
6. In each PR body:
   - describe the user-visible bug
   - describe only that PR's behavior change
   - call out any defaults introduced
   - include concrete examples
7. Avoid bundling docs, cleanup, and unrelated refactors into the bug-fix PRs.

## Recommended PR Order

1. Sonarr settings/UI for season thresholds
2. Sonarr threshold enforcement
3. Radarr strict upgrade preflight

This order gives the maintainer the least surprising review sequence.

## Notes For The Next Agent

- Prefer implementing and testing one PR branch at a time, not all changes in one working tree.
- Re-run live API probes as needed, but avoid touching the user's production containers.
- The user's production repo is separate: `/home/bradley/code/media-server-v2`
- The NeutArr work should stay in `/home/bradley/code/NeutArr`
- The live Radarr and Sonarr APIs are reachable locally and were already used for read-only verification.

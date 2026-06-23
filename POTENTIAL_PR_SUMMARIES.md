# Potential PR Summaries

These summaries are written for maintainers who want a clear problem statement, a conservative implementation approach, and enough detail to trust the change quickly.

## PR 1: Radarr strict upgrade preflight before triggering downloads

### Suggested title
`Radarr: preflight upgrade candidates and queue the best explicit release`

### Problem
NeutArr currently treats a cutoff-unmet Radarr movie as upgradeable without first confirming that Radarr can actually see a better downloadable release for that movie. In practice, that can lead to low-value upgrade cycles, unnecessary search commands, and ambiguous behavior when the current file and candidate releases differ only by custom format score.

### What this change does
- Fetches Radarr release candidates up front with `GET /api/v3/release?movieId=...`
- Builds a quality-profile ranking map so comparisons follow Radarr’s actual profile order instead of raw quality IDs
- Compares the current movie file against candidate releases using:
  - higher quality tier first
  - same-tier custom format improvement second
- Requires same-tier upgrades to satisfy `minUpgradeFormatScore`
- Queues the exact winning cached release with `POST /api/v3/release` instead of firing a broad `MoviesSearch`

### Why this is safer
This reduces guesswork. NeutArr only asks Radarr to act when Radarr has already exposed a concrete, better, downloadable release. That makes upgrade behavior more predictable and avoids spending cycles on titles that are technically cutoff-unmet but have no worthwhile candidate available right now.

## PR 2: Sonarr season-pack upgrade thresholds with matching UI controls

### Suggested title
`Sonarr: add configurable season-pack upgrade thresholds and expose them in settings`

### Problem
In Sonarr `Season Packs` upgrade mode, NeutArr can decide to search an entire season based on a small random sample of cutoff-unmet episodes. That works, but it can be too aggressive when only a tiny number of episodes in the season are actually below cutoff. The result is that full-season upgrade searches can trigger even when the season-wide upgrade value is weak.

### What this change does
- Adds two Sonarr settings:
  - `season_upgrade_min_cutoff_unmet_episodes`
  - `season_upgrade_min_cutoff_unmet_percent`
- Computes season eligibility from full aired season data, not just the random sample:
  - fetches all aired episodes for each sampled series
  - fetches all aired cutoff-unmet episodes for the same series
  - calculates per-season cutoff-unmet count and percentage
- Only allows a season-pack upgrade when both configured thresholds are met
- Adds the same settings to the Sonarr web UI
- Shows those UI fields only when `Upgrade Mode` is set to `Season Packs`, so the form stays relevant and uncluttered

### Why this is safer
This makes season-pack upgrades intentional rather than opportunistic. Users can tune how much of a season needs to be below cutoff before NeutArr spends a full season search on it, which should reduce noisy searches while preserving the value of season-pack mode for torrent-heavy setups.

## PR 3: Structured, AI-readable upgrade diagnostics for Radarr and Sonarr

### Suggested title
`Add structured upgrade decision logs for Radarr and Sonarr`

### Problem
When NeutArr makes upgrade decisions, the reasoning is hard to reconstruct from logs. The important state is often spread across many free-form lines, and large Radarr candidate sets can flood the log with low-signal noise. That makes live debugging, regression review, and automated analysis unnecessarily difficult.

### What this change does
- Adds bounded `AI_EVENT {json}` debug entries around upgrade decisions
- Uses stable event names and field names so downstream tools can parse them reliably
- Summarizes Radarr release evaluation with:
  - current file state
  - profile/cutoff information
  - candidate counts
  - skip counts
  - capped samples of rejected and accepted candidates
- Summarizes Sonarr upgrade runs with:
  - sampled episodes
  - aired-episode filtering results
  - selected seasons/series
  - threshold decisions
  - command completion events

### Why this is safer
The goal is observability, not behavior change. The logs make it easier to verify why NeutArr acted or did not act, without requiring maintainers to infer the decision path from scattered debug output. The data is intentionally capped so the added visibility does not explode into unbounded log spam.

## Notes

- The Sonarr backend threshold logic and Sonarr UI controls should stay in the same PR. Splitting them would make the feature harder to review and easier to misconfigure.
- The local `media-server-v2` dev-stack helpers are not part of these upstream PRs. They are only for local verification and iteration.

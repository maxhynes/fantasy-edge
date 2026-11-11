# Fantasy Edge: handoff notes for another AI assistant

Read this first. It explains what the project is, how it's built, what has been verified, and what's still open. The owner wants help improving or extending it.

## What it is

Fantasy Edge is a personal fantasy-sports assistant that runs locally on the owner's Mac. It runs a small Python web server and serves one HTML page.

- **Football:** Sleeper. Its public read-only API needs only a username. The owner follows several Sleeper leagues at once.
- **Hockey:** Yahoo Fantasy, through OAuth2 with the owner's own Yahoo developer app (`redirect_uri=https://localhost:8080`, `scope=fspt-r`; the owner pastes back the redirected URL). Player stats come from the public NHL API.
- **Goal:** help the owner win. It grades every team in each league against several "trackers", gives start/sit, waiver and trade advice, and runs **feedback loops** that score its own predictions every week and re-weight the trackers.

## Hard constraints (please keep these)

- **Python 3.9 standard library only.** No pip installs. macOS ships Python 3.9.6, so don't use `match`, `X | Y` type unions, or other 3.10+ syntax.
- **Frontend is one file** (`fantasy_edge/static/index.html`): vanilla JS with hand-written SVG charts, no build step, no CDNs.
- **Runs only on 127.0.0.1.** The server rejects API calls whose `Origin` isn't its own page.
- **Secrets stay local** in `fantasy_edge/data/config.json` (chmod 600). That folder is not included in this handoff.

## Run it

```bash
python3 -m fantasy_edge.server        # opens http://127.0.0.1:8765
NO_BROWSER=1 python3 -m fantasy_edge.server   # without auto-opening a browser
```

The `data/` folder (config, SQLite DB, HTTP cache) is created on first run.

## Files

| File | Role |
|---|---|
| `fantasy_edge/server.py` | `ThreadingHTTPServer` and every `/api/*` route. Caches each league's dashboard in memory for 10 minutes. Builds the slim `/api/home` summary (leagues fetched in parallel). Starts background feedback runs, at most every 6h per league. |
| `fantasy_edge/core.py` | `http_json` (disk cache by URL + TTL, falls back to a stale copy on network errors), config load/save, SQLite schema and helpers, Spearman, `quantile_map`, letter grades from z-scores, greedy `optimize_lineup`, `roster_strength`, 1-for-1 `trade_finder`, `blend` (weighted tracker average). |
| `fantasy_edge/football.py` | Sleeper `League` class: scores players with the league's own `scoring_settings`; builds trackers, grades, matchup win %, advice, trending players; `run_feedback()` backfills every finished week. |
| `fantasy_edge/hockey.py` | Yahoo OAuth (`yahoo_auth_url`, `yahoo_exchange`, `yget` with token refresh); parsers for Yahoo's nested JSON (`ymerge`, `ycoll`, `yobj`); NHL stats API (`nhl_stats` by date range, `team_games` from the schedule); Yahoo→NHL player matching by name and team; category z-scores; Monte-Carlo category matchup sims; daily lineup check; waivers; `run_feedback()`. |
| `fantasy_edge/feedback.py` | The four feedback loops plus `learned_weights()` and `report()`. |
| `fantasy_edge/static/index.html` | Tabs: Home (all leagues, charts), Football, Hockey, Feedback, Connect. |

## Trackers (the independent predictors)

Every tracker outputs **expected fantasy value for one week, in that league's scoring**, so they can be blended and compared.

- **Football:**
  - `proj`: Sleeper weekly projection.
  - `season`: season points per game before that week.
  - `recent`: average of the last 3 games played.
  - `market`: FantasyCalc trade value, quantile-mapped onto points **within each position**.
  - Players on a bye get 0 from every tracker. If `proj` is exactly 0 on a non-bye week, the player is treated as out.
- **Hockey:**
  - `season`: season per-game value × games this week.
  - `recent`: last-14-days per-game value × games this week.
  - `market`: Yahoo % rostered, quantile-mapped onto per-game value.
  - Per-game value is the sum over the league's categories of `rate / stdev(rate across ~400 regular skaters)`, a z-score style scale. GAA and SV% use `(x - mean) / sd`. Goalies are counted per start, times their expected share of starts. Points leagues use Yahoo's `stat_modifiers` instead.

## The feedback loops (`feedback.py`, tables in `core.SCHEMA`)

1. **Tracker accuracy → weights.**
   - Predictions go into `predictions`, results into `actuals`.
   - `score_week()` computes a Spearman rank correlation per tracker per week (`source_scores`).
   - `learned_weights()`: recency-weighted correlation (half-life 3 weeks), clipped at 0.02 and squared, blended with the `PRIOR` weights (prior is worth 2 weeks of evidence), then normalized.
   - Trackers that can be rebuilt from history overwrite old rows; snapshot-only trackers (`market`) keep their first snapshot.
2. **Matchup calibration.** Win probabilities go into `matchup_preds`. After the week, `set_matchup_result` fills in the result, and `report()` computes the Brier score and the share of winners picked.
3. **Advice outcomes.** Every start/sit and add/drop call goes into `advice`. `resolve_advice()` detects whether the owner followed it (from actual starters or later rosters) and records how many points it was worth.
4. **Lineup efficiency.** `lineup_eff` stores actual vs. best-possible points for every team, every week (football).

## Verified vs. not

- ✅ **Football:** tested end-to-end against live Sleeper leagues. Weeks 1–9 backfilled. Learned weights favor Sleeper projections (accuracy ~0.6–0.77 vs ~0.3–0.45 for the others). Win-probability Brier score ≈ 0.23–0.24, where a coin flip is 0.25.
- ⛔ **Yahoo access approval:** since July 2026 Yahoo requires each app to be approved at https://sports.yahoo.com/developer/access/. The owner's login works but every Fantasy API call returns 403 *"This application is not authorized to perform this action"* until approval comes through.
- ⚠️ **Hockey:** tested only against **Yahoo-shaped mock responses** plus live NHL stats. 190/190 players matched, and every feature produced output. It has **not been run against a real Yahoo account**. The Yahoo JSON parsing (`yobj`/`ycoll` paths in `_load_teams`, `_scoreboard`, `percent_owned`, `free_agents`, `yahoo_leagues`) is the most likely thing to need fixes on first real connect.

## Known limits / good next steps

1. Hockey lineups are judged per week, not per day. Daily slot limits on busy nights aren't modeled.
2. Goalie and skater values sit on different scales, so hockey trade ideas that swap a goalie for a skater are unreliable.
3. `optimize_lineup` is greedy (most restrictive slot first), not a true optimal assignment.
4. Football win % uses a normal approximation with sd = 22% of the average projected score. That could be calibrated from the Loop 2 data.
5. Advice gets logged on every dashboard load (deduplicated by a unique key), so the "calls made" count can run high.
6. No tests yet. Pure functions in `core.py` and `feedback.py` would be easy to cover with `unittest`.
7. Possible extra trackers: other projection sources, Vegas implied team totals, injury news.

## API routes (all JSON)

```
GET  /api/status
GET  /api/home[?force=1]
POST /api/sleeper/connect      {username}        -> leagues
POST /api/sleeper/leagues      {leagues:[{id,name}]}
GET  /api/nfl/dashboard?league=ID[&force=1]
GET  /api/nfl/feedback?league=ID   (POST = run backfill/scoring now)
POST /api/yahoo/config         {client_id, client_secret, redirect}
GET  /api/yahoo/auth_url
POST /api/yahoo/code           {code}            -> leagues
GET  /api/yahoo/leagues
POST /api/yahoo/league         {id, name}
GET  /api/nhl/dashboard[?force=1]
GET  /api/nhl/feedback          (POST = run now)
```

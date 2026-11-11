# Fantasy Edge

> **Personal, non-commercial project.** Fantasy Edge runs locally on its owner's computer to help manage their own fantasy teams. It only **reads** league data (no writes back to any platform), stores everything on the local machine, and isn't distributed as a hosted service.
>
> **Data sources:** Sleeper's public API (football), the Yahoo Fantasy Sports API with read-only OAuth access (hockey: league settings, teams, rosters, standings, matchups, available players), the public NHL stats API, and FantasyCalc trade values.

A personal fantasy assistant that runs on your Mac. It connects to **Sleeper (football)** and **Yahoo (hockey)**, grades every team in your league against several trackers, gives you start/sit, waiver and trade calls, and checks its own accuracy every week so it keeps getting better.

No installs needed beyond the Python 3 that ships with macOS.

## Start it

Double-click **`start.command`**, or run:

```bash
python3 -m fantasy_edge.server
```

Your browser opens at http://127.0.0.1:8765. Leave the terminal window open while you use it.

## Connect your accounts (one time)

**Sleeper (football):** open the Connect tab, type your Sleeper username, pick your league. You don't need a password because Sleeper's API is public and read-only.

**Yahoo (hockey):** Yahoo requires a free developer key.
1. Go to https://developer.yahoo.com/apps/create/
   - Application Name: anything (e.g. "My Fantasy Edge")
   - OAuth Client Type: **Confidential Client**
   - Redirect URI(s): **https://localhost:8080**
   - API Permissions: **Fantasy Sports → Read**
2. Copy the **Client ID** and **Client Secret** into the Connect tab and click *Save & get login link*.
3. Click *Copy link*, open it in your normal browser, tick the terms box and click **Next** (not Skip).
4. The browser lands on a page that won't load. That's expected: copy the whole address (it contains `code=`) and paste it into the app. It connects automatically; then pick your league.

**Note:** since mid-2026 Yahoo also requires each app to be approved for Fantasy Sports API access at https://sports.yahoo.com/developer/access/. Until approval, every request returns *403 "This application is not authorized to perform this action."*

## What you get

| Section | What it shows |
|---|---|
| **League power grades** | Every team graded A+ to F for *this week* and *rest of season*, plus a grade from each tracker separately and by position (football) or by scoring category (hockey). Click a team to see its full roster with every tracker's number for every player. |
| **Matchups** | Win probability for every game. Hockey category leagues are simulated category by category. |
| **Start / sit** | Football: the best lineup compared with the one you set. Hockey: "today's lineup check" flags benched players who have a game while a starter has none. |
| **Waiver targets** | Free agents who beat your weakest player at that spot for the rest of the season. |
| **Win-win trades** | 1-for-1 trades that improve both your lineup and theirs, which makes them easier to get accepted. |
| **Trending** | The most-added players on Sleeper, and whether they're available in your league. |

### The trackers

| Football (Sleeper) | Hockey (Yahoo) |
|---|---|
| Sleeper weekly projections | Season pace (NHL stats × games this week) |
| Season points per game | Last 14 days (hot/cold) |
| Last 3 games | Yahoo % rostered (what every Yahoo manager thinks) |
| FantasyCalc trade value (real-trade market) | |

All values use **your league's** scoring settings or categories.

## The feedback loops (Feedback tab)

1. **Which trackers are right?** Each tracker's predictions are logged every week and scored against what actually happened. Trackers that predict better get more weight in next week's grades. Football past weeks are rebuilt from history, so the model is already trained the first time you open it.
2. **Are the win % calls honest?** Win probabilities are scored after each week (Brier score: lower is better, a coin flip is 0.25).
3. **Did the advice work?** Every start/sit and waiver call is logged. The app detects whether you followed it and records how many points it was worth, including what you lost by ignoring good calls.
4. **Lineup efficiency.** Points left on the bench each week, for every team in your league (football).

Updates run automatically in the background when you open a dashboard (at most every 6 hours), or on demand with the button.

## Files

```
start.command             double-click launcher
fantasy_edge/
  server.py               local web server + API routes
  football.py             Sleeper + FantasyCalc: trackers, grades, advice, backfill
  hockey.py               Yahoo OAuth + NHL stats: trackers, category grades, sims, advice
  feedback.py             the four feedback loops and the learned tracker weights
  core.py                 HTTP cache, SQLite, lineup optimizer, grading, trade finder
  static/index.html       the dashboard (single page, no build step)
  data/                   created on first run: config.json (your keys), edge.db (history), cache/
```

## Known limits

- Hockey lineups are judged per week, not per day. Daily slot limits on busy nights aren't modeled.
- Goalies and skaters are on different value scales, so hockey trade ideas that swap a goalie for a skater are less reliable.
- Snapshot trackers (FantasyCalc, Yahoo % rostered) have no history, so they only start being scored from the first week you use the app.
- The Yahoo side was tested against Yahoo-format sample data plus live NHL stats, not a live Yahoo account. If anything fails on first connect, the error shows on screen.

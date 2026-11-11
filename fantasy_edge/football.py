"""Football via Sleeper (public read-only API, just needs your username).

Trackers (each predicts a player's points for a week in YOUR league's scoring):
  proj    Sleeper's weekly projection
  season  season-to-date points per game (before that week)
  recent  last-3-games points per game
  market  FantasyCalc trade value (crowd-sourced from real trades), mapped to points
"""
from .core import (ApiError, blend, grade_teams, http_json, mean, norm_cdf, optimize_lineup,
                   quantile_map, trade_finder)
from . import feedback as fb

S = "https://api.sleeper.app"
POS = ["QB", "RB", "WR", "TE", "K", "DEF"]
PAST = 7 * 86400  # finished weeks never change
FLEX = {"FLEX": {"RB", "WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}, "REC_FLEX": {"WR", "TE"},
        "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "IDP_FLEX": {"DL", "LB", "DB"}}
NON_STARTING = {"BN", "IR", "TAXI"}
SOURCES = ["proj", "season", "recent", "market"]


def eligible(slot, pos):
    return bool(FLEX.get(slot, {slot}) & set(pos))


def state():
    return http_json(S + "/v1/state/nfl", ttl=900)


def user_leagues(username):
    u = http_json(S + "/v1/user/" + username.strip(), ttl=86400)
    if not u:
        raise ApiError("Sleeper user '%s' not found" % username)
    season = state()["season"]
    leagues = http_json("%s/v1/user/%s/leagues/nfl/%s" % (S, u["user_id"], season), ttl=3600) or []
    return u, [{"id": l["league_id"], "name": l["name"], "season": l["season"],
                "teams": l.get("total_rosters")} for l in leagues]


def players_db():
    return http_json(S + "/v1/players/nfl", ttl=86400)


def _weekly(kind, season, week):
    q = "&".join("position[]=" + p for p in POS)
    st = state()
    live = str(season) == st["season"] and week >= st["week"]
    path = "projections" if kind == "proj" else "stats"
    return http_json("%s/%s/nfl/%s/%s?season_type=regular&%s" % (S, path, season, week, q),
                     ttl=1800 if live else PAST) or []


def score(stats, scoring):
    return sum((stats.get(k) or 0) * v for k, v in scoring.items() if isinstance(v, (int, float)))


class League:
    def __init__(self, league_id, username):
        self.id = league_id
        self.st = state()
        self.week = max(1, int(self.st["display_week"] or self.st["week"] or 1))
        self.league = http_json("%s/v1/league/%s" % (S, league_id), ttl=3600)
        self.season = self.league["season"]
        self.scoring = self.league.get("scoring_settings") or {}
        self.slots = [s for s in self.league["roster_positions"] if s not in NON_STARTING]
        self.users = {u["user_id"]: u for u in http_json("%s/v1/league/%s/users" % (S, league_id), ttl=3600)}
        self.rosters = http_json("%s/v1/league/%s/rosters" % (S, league_id), ttl=600)
        self.pdb = players_db()
        me = http_json(S + "/v1/user/" + username.strip(), ttl=86400)
        self.my_roster = next((r["roster_id"] for r in self.rosters if r.get("owner_id") == me["user_id"]
                               or me["user_id"] in (r.get("co_owners") or [])), None)
        self.names = {}
        for r in self.rosters:
            u = self.users.get(r.get("owner_id")) or {}
            self.names[r["roster_id"]] = (u.get("metadata") or {}).get("team_name") or u.get("display_name") or \
                "Team %s" % r["roster_id"]
        self._stats = {}
        self._proj = {}

    # ------------------------------------------------------------------ data
    def pinfo(self, pid):
        p = self.pdb.get(pid) or {}
        name = p.get("full_name") or ("%s %s" % (p.get("first_name", ""), p.get("last_name", ""))).strip() or pid
        return {"id": pid, "name": name, "pos": p.get("fantasy_positions") or [p.get("position") or "?"],
                "team": p.get("team") or "FA", "injury": p.get("injury_status")}

    def week_points(self, kind, week):
        cache = self._stats if kind == "stats" else self._proj
        if week not in cache:
            out, teams = {}, set()
            for row in _weekly(kind, self.season, week):
                st = row.get("stats") or {}
                if kind == "stats" and not st.get("gp"):
                    continue
                if row.get("team"):
                    teams.add(row["team"])
                out[row["player_id"]] = round(score(st, self.scoring), 2)
            cache[week] = (out, teams)
        return cache[week]

    def market_values(self):
        s = self.league.get("settings") or {}
        rec = self.scoring.get("rec", 0)
        sf = "SUPER_FLEX" in self.slots or self.slots.count("QB") > 1
        url = ("https://api.fantasycalc.com/values/current?isDynasty=%s&numQbs=%d&numTeams=%d&ppr=%s"
               % ("true" if s.get("type") == 2 else "false", 2 if sf else 1,
                  self.league.get("total_rosters") or 12, rec))
        try:
            vals = http_json(url, ttl=6 * 3600)
        except ApiError:
            return {}
        return {str(v["player"]["sleeperId"]): v["value"] for v in vals if v["player"].get("sleeperId")}

    # ------------------------------------------------------------- trackers
    def tracker_preds(self, week, with_market=False):
        """{pid: {source: predicted points}} for `week`, using only info from before it."""
        proj, playing = self.week_points("proj", week)
        hist = {}
        for w in range(1, week):
            pts, _ = self.week_points("stats", w)
            for pid, v in pts.items():
                hist.setdefault(pid, []).append(v)
        preds = {}
        pids = set(proj) | set(hist)
        for pid in pids:
            h = hist.get(pid, [])
            preds[pid] = {"proj": proj.get(pid),
                          "season": round(mean(h), 2) if h else None,
                          "recent": round(mean(h[-3:]), 2) if h else None}
        if with_market:
            mv = self.market_values()
            for pos in POS:  # map trade value -> points within each position
                ids = {k for k in mv if pos in self.pinfo(k)["pos"][:1]}
                ref = [p["season"] for k, p in preds.items() if p.get("season") and pos in self.pinfo(k)["pos"][:1]]
                for pid, v in quantile_map({k: mv[k] for k in ids}, ref).items():
                    preds.setdefault(pid, {})["market"] = round(v, 2)
        # bye / not playing this week -> zero for every tracker
        for pid, src in preds.items():
            team = (self.pdb.get(pid) or {}).get("team")
            if playing and team and team not in playing:
                for s in src:
                    if src[s] is not None:
                        src[s] = 0.0
        return preds

    # --------------------------------------------------------- main payload
    def dashboard(self):
        week = self.week
        weights, wdetail = fb.learned_weights("nfl", self.id)
        preds = self.tracker_preds(week, with_market=True)
        fb.log_predictions("nfl", self.id, week, {p: v for p, v in preds.items()},
                           replace_sources=("proj", "season", "recent"))
        ros_weights = {"proj": weights["proj"] * 0.4, "season": weights["season"],
                       "recent": weights["recent"], "market": weights["market"] * 1.5}
        rostered = {}
        teams_week, teams_ros, per_source = {}, {}, {s: {} for s in SOURCES}
        team_players = {}
        hist_pre = self.tracker_preds(week + 1)  # includes this week's games for ROS
        for r in self.rosters:
            rid = r["roster_id"]
            plist = []
            for pid in r.get("players") or []:
                rostered[pid] = rid
                info = self.pinfo(pid)
                src = preds.get(pid, {})
                info["trackers"] = src
                # Sleeper projecting exactly 0 on a non-bye week = expected out
                info["value"] = 0.0 if src.get("proj") == 0 else round(blend(src, weights), 2)
                ros_src = dict(hist_pre.get(pid, {}))
                ros_src["market"] = src.get("market")
                ros_src["proj"] = src.get("proj") if src.get("proj") else ros_src.get("proj")
                info["ros"] = round(blend(ros_src, ros_weights), 2)
                info["starter"] = pid in (r.get("starters") or [])
                info["reserve"] = pid in (r.get("reserve") or []) or pid in (r.get("taxi") or [])
                plist.append(info)
            team_players[rid] = plist
            active = [p for p in plist if not p["reserve"]]
            teams_week[rid], _ = optimize_lineup(active, self.slots, eligible, "value")
            teams_ros[rid], _ = optimize_lineup(active, self.slots, eligible, "ros")
            for s in SOURCES:
                tmp = [dict(p, v=(p["trackers"].get(s) or 0)) for p in active]
                per_source[s][rid], _ = optimize_lineup(tmp, self.slots, eligible, "v")

        # positional grades (best starters at each base position)
        pos_grades = {}
        for pos in ["QB", "RB", "WR", "TE"]:
            n = max(1, sum(1 for s in self.slots if s == pos))
            vals = {}
            for rid, pl in team_players.items():
                best = sorted((p["ros"] for p in pl if pos in p["pos"] and not p["reserve"]), reverse=True)
                vals[rid] = sum(best[:n + 1])
            pos_grades[pos] = grade_teams(vals)

        g_week, g_ros = grade_teams(teams_week), grade_teams(teams_ros)
        g_src = {s: grade_teams(v) for s, v in per_source.items() if any(v.values())}
        teams = []
        for r in self.rosters:
            rid = r["roster_id"]
            st = r.get("settings") or {}
            teams.append({"id": rid, "name": self.names[rid], "mine": rid == self.my_roster,
                          "record": "%s-%s%s" % (st.get("wins", 0), st.get("losses", 0),
                                                 "-%s" % st["ties"] if st.get("ties") else ""),
                          "pf": round((st.get("fpts") or 0) + (st.get("fpts_decimal") or 0) / 100, 1),
                          "week": g_week[rid], "ros": g_ros[rid],
                          "trackers": {s: g[rid] for s, g in g_src.items()},
                          "positions": {p: g[rid]["grade"] for p, g in pos_grades.items()},
                          "players": sorted(team_players[rid], key=lambda p: -p["ros"])})
        teams.sort(key=lambda t: t["ros"]["rank"])

        # matchups + win probability
        matchups = http_json("%s/v1/league/%s/matchups/%s" % (S, self.id, week), ttl=300) or []
        mids = {}
        for m in matchups:
            if m.get("matchup_id"):
                mids.setdefault(m["matchup_id"], []).append(m["roster_id"])
        games = []
        for mid, pair in mids.items():
            if len(pair) != 2:
                continue
            a, b = pair
            pa, pb = teams_week[a], teams_week[b]
            sd = 0.22 * (pa + pb) / 2 * 1.414 or 1
            prob = norm_cdf((pa - pb) / sd)
            fb.log_matchup("nfl", self.id, week, a, b, round(prob, 3))
            fb.log_matchup("nfl", self.id, week, b, a, round(1 - prob, 3))
            games.append({"a": self.names[a], "b": self.names[b], "a_proj": round(pa, 1), "b_proj": round(pb, 1),
                          "a_win": round(prob, 3), "mine": self.my_roster in pair})

        advice = self.advice(week, team_players, preds, rostered)
        trades = []
        if self.my_roster:
            tp = {rid: [p for p in pl if not p["reserve"]] for rid, pl in team_players.items()}
            for t in trade_finder(self.my_roster, tp, self.slots, eligible, "ros"):
                t["team"] = self.names[t["team_id"]]
                trades.append(t)
        return {"sport": "nfl", "league": {"id": self.id, "name": self.league["name"], "season": self.season,
                                            "week": week, "slots": self.slots},
                "weights": weights, "weight_detail": wdetail, "teams": teams, "games": games,
                "advice": advice, "trades": trades, "trending": self.trending(rostered)}

    def advice(self, week, team_players, preds, rostered):
        out = {"start_sit": [], "waivers": []}
        if not self.my_roster:
            return out
        weights, _ = fb.learned_weights("nfl", self.id)
        mine = [p for p in team_players[self.my_roster] if not p["reserve"]]
        _, best = optimize_lineup(mine, self.slots, eligible, "value")
        best_ids = {p["id"] for _, p in best if p}
        cur = [p for p in mine if p["starter"]]
        bench_in = sorted([p for p in mine if p["id"] in best_ids and not p["starter"]], key=lambda p: -p["value"])
        sit = sorted([p for p in cur if p["id"] not in best_ids], key=lambda p: p["value"])
        for a, b in zip(bench_in, sit):
            edge = round(a["value"] - b["value"], 2)
            txt = "Start %s over %s (+%.1f proj pts)" % (a["name"], b["name"], edge)
            out["start_sit"].append({"start": a["name"], "sit": b["name"], "edge": edge, "text": txt})
            fb.log_advice("nfl", self.id, week, "start", txt, a["id"], b["id"], edge)
        # waivers: best free agents vs. your weakest player at that position (ROS)
        fa = []
        hist_pre = self.tracker_preds(week + 1)
        for pid, src in preds.items():
            if pid in rostered or pid not in self.pdb:
                continue
            info = self.pinfo(pid)
            if info["team"] == "FA" or not any(eligible(sl, info["pos"]) for sl in self.slots):
                continue
            ros_src = dict(hist_pre.get(pid, {}))
            ros_src["market"] = src.get("market")
            ros = blend(ros_src, weights)
            if ros <= 0:
                continue
            same = [p for p in mine if set(p["pos"]) & set(info["pos"])]
            worst = min(same, key=lambda p: p["ros"]) if same else None
            gain = ros - (worst["ros"] if worst else 0)
            if gain > 0.5:
                fa.append((gain, info, ros, worst))
        fa.sort(key=lambda x: -x[0])
        for gain, info, ros, worst in fa[:12]:
            txt = "Add %s (%s) %s" % (info["name"], info["pos"][0],
                                      "drop %s" % worst["name"] if worst else "")
            out["waivers"].append({"add": info, "drop": worst and worst["name"], "ros": round(ros, 2),
                                   "gain": round(gain, 2), "text": txt})
            fb.log_advice("nfl", self.id, week, "add", txt, info["id"], worst["id"] if worst else "", round(gain, 2))
        return out

    def trending(self, rostered):
        try:
            tr = http_json(S + "/v1/players/nfl/trending/add?lookback_hours=48&limit=25", ttl=1800)
        except ApiError:
            return []
        return [dict(self.pinfo(t["player_id"]), adds=t["count"], available=t["player_id"] not in rostered)
                for t in tr]

    # ------------------------------------------------------- feedback update
    def run_feedback(self):
        """Backfill + score every finished week this season. Safe to re-run."""
        done = []
        last_done = self.week - 1 if self.st.get("season_type") == "regular" else self.week
        for w in range(1, last_done + 1):
            matchups = http_json("%s/v1/league/%s/matchups/%s" % (S, self.id, w), ttl=PAST) or []
            if not matchups:
                continue
            preds = self.tracker_preds(w)
            rostered_pts = {}
            for m in matchups:
                rostered_pts.update(m.get("players_points") or {})
            fb.log_predictions("nfl", self.id, w,
                               {p: preds[p] for p in rostered_pts if p in preds},
                               replace_sources=("proj", "season", "recent"))
            fb.log_actuals("nfl", self.id, w, rostered_pts)
            fb.score_week("nfl", self.id, w)

            # matchup calibration (retro-projected if we never saw the week live)
            weights, _ = fb.learned_weights("nfl", self.id)
            by_mid = {}
            eff = []
            for m in matchups:
                by_mid.setdefault(m.get("matchup_id"), []).append(m)
                pl = [dict(self.pinfo(p), value=(m.get("players_points") or {}).get(p, 0))
                      for p in m.get("players") or []]
                opt, _ = optimize_lineup(pl, self.slots, eligible, "value")
                eff.append((m["roster_id"], self.names.get(m["roster_id"]), round(m.get("points") or 0, 2),
                            round(opt, 2)))
            fb.log_lineup_eff("nfl", self.id, w, eff)
            for mid, pair in by_mid.items():
                if not mid or len(pair) != 2:
                    continue
                a, b = pair
                proj = {}
                for m in pair:
                    pl = [dict(self.pinfo(p), value=blend(preds.get(p, {}), weights)) for p in m.get("players") or []]
                    proj[m["roster_id"]], _ = optimize_lineup(pl, self.slots, eligible, "value")
                pa, pb = proj[a["roster_id"]], proj[b["roster_id"]]
                prob = norm_cdf((pa - pb) / (0.22 * (pa + pb) / 2 * 1.414 or 1))
                res = 1.0 if a["points"] > b["points"] else 0.0 if a["points"] < b["points"] else 0.5
                fb.log_matchup("nfl", self.id, w, a["roster_id"], b["roster_id"], round(prob, 3))
                fb.log_matchup("nfl", self.id, w, b["roster_id"], a["roster_id"], round(1 - prob, 3))
                fb.set_matchup_result("nfl", self.id, w, a["roster_id"], res)
                fb.set_matchup_result("nfl", self.id, w, b["roster_id"], 1 - res)

            mine = next((m for m in matchups if m["roster_id"] == self.my_roster), None)
            if mine:
                pts = mine.get("players_points") or {}
                starters = set(mine.get("starters") or [])
                roster_ids = set(mine.get("players") or [])
                later = set(next((r for r in self.rosters if r["roster_id"] == self.my_roster), {}).get("players") or [])

                def followed(r):
                    if r["kind"] == "start":
                        return int(r["player_a"] in starters and r["player_b"] not in starters)
                    return int(r["player_a"] in later)

                def outcome(r):
                    if r["kind"] == "start":
                        if r["player_a"] in pts and r["player_b"] in pts:
                            return round(pts[r["player_a"]] - pts[r["player_b"]], 2)
                        return None
                    stats, _ = self.week_points("stats", w)
                    a = stats.get(r["player_a"], 0)
                    b = pts.get(r["player_b"], stats.get(r["player_b"], 0)) if r["player_b"] else 0
                    return round(a - b, 2)
                fb.resolve_advice("nfl", self.id, w, followed, outcome)
            done.append(w)
        return {"weeks_scored": done}

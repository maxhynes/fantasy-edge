"""Hockey via Yahoo Fantasy (OAuth) + the public NHL stats API.

Trackers (each predicts a player's fantasy value for a week, in YOUR league's
categories or points):
  season  season-to-date per-game production x games that week
  recent  last-14-days per-game production x games that week (hot/cold)
  market  Yahoo % rostered (what every Yahoo manager thinks), mapped to value
"""
import base64
import datetime as dt
import random
import time
import urllib.parse

from .core import (ApiError, blend, grade_teams, http_json, load_config, mean, norm_name,
                   optimize_lineup, quantile_map, stdev, trade_finder, update_config)
from . import feedback as fb

YA = "https://api.login.yahoo.com/oauth2"
YF = "https://fantasysports.yahooapis.com/fantasy/v2/"
NS = "https://api.nhle.com/stats/rest/en"
NW = "https://api-web.nhle.com/v1"
PAST = 7 * 86400
SOURCES = ["season", "recent", "market"]
NON_STARTING = {"BN", "IR", "IR+", "IRLT", "NA"}
OUT_STATUS = {"IR", "IR-LT", "IR-NR", "O", "NA", "SUSP"}

# Yahoo display name -> (group, nhl field, direction)   group: S skater, G goalie, GR goalie ratio
CATS = {
    "G": ("S", "goals", 1), "A": ("S", "assists", 1), "P": ("S", "points", 1),
    "+/-": ("S", "plusMinus", 1), "PIM": ("S", "penaltyMinutes", 1), "PPP": ("S", "ppPoints", 1),
    "PPG": ("S", "ppGoals", 1), "PPA": ("S", "ppAssists", 1), "SHP": ("S", "shPoints", 1),
    "SHG": ("S", "shGoals", 1), "GWG": ("S", "gameWinningGoals", 1), "SOG": ("S", "shots", 1),
    "FW": ("S", "totalFaceoffWins", 1), "FL": ("S", "totalFaceoffLosses", -1), "HIT": ("S", "hits", 1),
    "BLK": ("S", "blockedShots", 1), "TK": ("S", "takeaways", 1), "STK": ("S", "takeaways", 1),
    "W": ("G", "wins", 1), "L": ("G", "losses", -1), "GA": ("G", "goalsAgainst", -1),
    "SV": ("G", "saves", 1), "SA": ("G", "shotsAgainst", 1), "SHO": ("G", "shutouts", 1),
    "GS": ("G", "gamesStarted", 1), "GAA": ("GR", "gaa", -1), "SV%": ("GR", "svpct", 1),
}
TEAM_FIX = {"NJ": "NJD", "TB": "TBL", "LA": "LAK", "SJ": "SJS", "MON": "MTL", "WAS": "WSH",
            "CLS": "CBJ", "CAL": "CGY", "NAS": "NSH", "ANH": "ANA", "UTAH": "UTA", "ARI": "UTA"}


# ============================================================== Yahoo OAuth
def yahoo_auth_url():
    c = load_config()
    if not c.get("yahoo_client_id"):
        raise ApiError("Add your Yahoo Client ID and Secret first.")
    return YA + "/request_auth?" + urllib.parse.urlencode({
        "client_id": c["yahoo_client_id"], "redirect_uri": c.get("yahoo_redirect", "oob"),
        "response_type": "code", "language": "en-us",
        "scope": "fspt-r"})  # Fantasy Sports read; newer Yahoo apps need it requested explicitly


def _token_call(params):
    c = load_config()
    basic = base64.b64encode(("%s:%s" % (c["yahoo_client_id"], c["yahoo_client_secret"])).encode()).decode()
    params["redirect_uri"] = c.get("yahoo_redirect", "oob")
    tok = http_json(YA + "/get_token", ttl=0, data=urllib.parse.urlencode(params).encode(), method="POST",
                    headers={"Authorization": "Basic " + basic,
                             "Content-Type": "application/x-www-form-urlencoded"})
    update_config(yahoo_scope=tok.get("scope"), yahoo_token_fields=sorted(tok.keys()),  # names only, for diagnosis
                  yahoo_access=tok["access_token"],
                  yahoo_refresh=tok.get("refresh_token") or c.get("yahoo_refresh"),
                  yahoo_expires=time.time() + int(tok.get("expires_in", 3600)) - 60)


def yahoo_exchange(code_or_url):
    code = code_or_url.strip()
    if "code=" in code:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [""])[0]
    _token_call({"grant_type": "authorization_code", "code": code})


def yget(path, ttl=300):
    c = load_config()
    if not c.get("yahoo_refresh"):
        raise ApiError("Yahoo is not connected yet.")
    if time.time() > c.get("yahoo_expires", 0):
        _token_call({"grant_type": "refresh_token", "refresh_token": c["yahoo_refresh"]})
        c = load_config()
    sep = "&" if "?" in path else "?"
    try:
        return http_json(YF + path + sep + "format=json", ttl=ttl,
                         headers={"Authorization": "Bearer " + c["yahoo_access"]})["fantasy_content"]
    except ApiError as e:
        if "HTTP 403" in str(e) and "not authorized" in str(e):
            raise ApiError("Yahoo says this login doesn't include Fantasy Sports access. Make sure your Yahoo app has "
                           "'Fantasy Sports - Read' checked, then reconnect on the Connect tab (Copy link -> Agree -> paste).")
        raise


def ymerge(x):
    out = {}
    if isinstance(x, list):
        for it in x:
            if isinstance(it, dict):
                out.update(it)
            elif isinstance(it, list):
                out.update(ymerge(it))
    elif isinstance(x, dict):
        out.update(x)
    return out


def ycoll(d, key):
    if not isinstance(d, dict):
        return []
    return [d[k][key] for k in sorted((k for k in d if k.isdigit()), key=int)
            if isinstance(d[k], dict) and key in d[k]]


def yobj(lst):
    """Yahoo 'team'/'player' arrays: [meta-list, {extra}, {extra}...] -> one dict"""
    if not isinstance(lst, list):
        return dict(lst)
    if lst and isinstance(lst[0], list):
        out = ymerge(lst[0])
        for x in lst[1:]:
            if isinstance(x, dict):
                out.update(x)
        return out
    return ymerge(lst)


def yahoo_leagues():
    fc = yget("users;use_login=1/games;game_codes=nhl/leagues", ttl=3600)
    user = fc["users"]["0"]["user"]
    games = ycoll(ymerge(user[1:]).get("games"), "game")
    out = []
    for g in games:
        gm = yobj(g)
        for l in ycoll(gm.get("leagues"), "league"):
            lm = ymerge(l)
            out.append({"id": lm["league_key"], "name": lm["name"], "season": lm.get("season"),
                        "teams": lm.get("num_teams")})
    out.sort(key=lambda l: str(l["season"]), reverse=True)
    return out


# ================================================================ NHL data
def _range_ttl(end):
    return PAST if end and end < dt.date.today().isoformat() else 1800


def nhl_stats(season_id, start=None, end=None):
    """Totals per NHL player for a date range (or whole season)."""
    exp = "gameTypeId=2 and seasonId=%s" % season_id
    if start:
        exp += ' and gameDate>="%s"' % start
    if end:
        exp += ' and gameDate<="%s"' % end
    agg = "true" if (start or end) else "false"
    q = "isAggregate=%s&isGame=false&limit=-1&cayenneExp=%s" % (agg, urllib.parse.quote(exp))
    ttl = _range_ttl(end) if end else 3600
    sk, go = {}, {}
    for rpt in ("summary", "realtime", "faceoffwins"):
        for r in http_json("%s/skater/%s?%s" % (NS, rpt, q), ttl=ttl).get("data", []):
            d = sk.setdefault(r["playerId"], {})
            for k, v in r.items():
                if v is not None:
                    d[k] = v
    for r in http_json("%s/goalie/summary?%s" % (NS, q), ttl=ttl).get("data", []):
        go[r["playerId"]] = r
    for d in sk.values():
        d["ppAssists"] = (d.get("ppPoints") or 0) - (d.get("ppGoals") or 0)
    for g in go.values():
        gs = g.get("gamesStarted") or 0
        g["gaa"] = g.get("goalsAgainstAverage") or 0
        g["svpct"] = g.get("savePct") or 0
        g["gs"] = gs
    return sk, go


def team_games(start, end):
    """{team_abbrev: games} between two ISO dates inclusive."""
    counts, day = {}, dt.date.fromisoformat(start)
    stop = dt.date.fromisoformat(end)
    while day <= stop:
        wk = http_json("%s/schedule/%s" % (NW, day.isoformat()), ttl=_range_ttl(end) if end else 6 * 3600)
        for gd in wk.get("gameWeek", []):
            if start <= gd["date"] <= end:
                for g in gd.get("games", []):
                    if g.get("gameType") == 2:
                        for side in ("awayTeam", "homeTeam"):
                            a = g[side]["abbrev"]
                            counts[a] = counts.get(a, 0) + 1
        day += dt.timedelta(days=7)
    return counts


def team_abbrev(row):
    return (row.get("teamAbbrevs") or "").split(",")[-1].strip()


# ============================================================= the league
class League:
    def __init__(self, league_key):
        self.key = league_key
        fc = yget("league/%s/settings" % league_key, ttl=3600)
        self.meta = ymerge(fc["league"][0])
        st = ymerge(fc["league"][1]["settings"])
        self.season = int(self.meta["season"])
        self.season_id = "%d%d" % (self.season, self.season + 1)
        self.week = int(self.meta.get("current_week") or 1)
        self.points_league = "point" in (self.meta.get("scoring_type") or "")
        self.stat_names, self.cats = {}, []
        for s in (st.get("stat_categories") or {}).get("stats", []):
            s = s["stat"]
            self.stat_names[str(s["stat_id"])] = s.get("display_name")
            if str(s.get("enabled", "1")) == "1" and not s.get("is_only_display_stat") \
                    and s.get("display_name") in CATS:
                self.cats.append(s["display_name"])
        self.mods = {}
        for s in (st.get("stat_modifiers") or {}).get("stats", []):
            name = self.stat_names.get(str(s["stat"]["stat_id"]))
            if name in CATS:
                self.mods[name] = float(s["stat"]["value"])
        self.slots = []
        for rp in st.get("roster_positions", []):
            rp = rp["roster_position"]
            if rp["position"] not in NON_STARTING:
                self.slots += [rp["position"]] * int(rp.get("count", 1))
        gw = yget("game/%s/game_weeks" % league_key.split(".")[0], ttl=86400)
        self.weeks = {}
        for w in ycoll(ymerge(gw["game"][1:]).get("game_weeks"), "game_week"):
            self.weeks[int(w["week"])] = (w["start"], w["end"])
        self.season_start = self.weeks[min(self.weeks)][0] if self.weeks else "%d-10-01" % self.season
        self._load_teams()
        self.sk, self.go = nhl_stats(self.season_id)
        self._index()
        self._build_scales()

    # ------------------------------------------------------------ rosters
    def _load_teams(self):
        fc = yget("league/%s/teams/roster" % self.key, ttl=300)
        self.teams = {}
        for t in ycoll(fc["league"][1]["teams"], "team"):
            tm = yobj(t)
            roster = tm.get("roster") or {}
            plist = ycoll((roster.get("0") or {}).get("players"), "player")
            players = []
            for p in plist:
                pm = yobj(p)
                sel = ymerge(pm.get("selected_position")).get("position")
                elig = [e["position"] for e in pm.get("eligible_positions", []) if isinstance(e, dict)]
                players.append({"key": pm["player_key"], "id": pm["player_key"], "name": pm["name"]["full"],
                                "yteam": (pm.get("editorial_team_abbr") or "").upper(),
                                "pos": elig or (pm.get("display_position") or "").split(","),
                                "display_pos": pm.get("display_position"),
                                "is_goalie": pm.get("position_type") == "G",
                                "status": pm.get("status"), "slot": sel})
            mine = str(tm.get("is_owned_by_current_login", "0")) == "1"
            mgr = ymerge([m.get("manager") for m in tm.get("managers", []) if isinstance(m, dict)])
            self.teams[tm["team_key"]] = {"name": tm["name"], "mine": mine, "players": players,
                                          "manager": mgr.get("nickname")}
        self.my_team = next((k for k, t in self.teams.items() if t["mine"]), None)

    def _index(self):
        self.by_name = {}
        for pid, r in self.sk.items():
            self.by_name.setdefault(norm_name(r.get("skaterFullName")), []).append(("S", pid))
        for pid, r in self.go.items():
            self.by_name.setdefault(norm_name(r.get("goalieFullName")), []).append(("G", pid))
        self.by_last = {}
        for (grp, src) in (("S", self.sk), ("G", self.go)):
            for pid, r in src.items():
                self.by_last.setdefault((norm_name(r.get("lastName")), team_abbrev(r)), []).append((grp, pid))

    def match(self, p):
        grp = "G" if p["is_goalie"] else "S"
        cands = [c for c in self.by_name.get(norm_name(p["name"]), []) if c[0] == grp]
        if len(cands) > 1:
            tm = TEAM_FIX.get(p["yteam"], p["yteam"])
            src = self.go if grp == "G" else self.sk
            cands = [c for c in cands if team_abbrev(src[c[1]]) == tm] or cands
        if not cands:
            last = norm_name(p["name"].split(" ")[-1])
            cands = [c for c in self.by_last.get((last, TEAM_FIX.get(p["yteam"], p["yteam"])), []) if c[0] == grp]
        return cands[0][1] if cands else None

    # ------------------------------------------------------------- values
    def _build_scales(self):
        sk = [r for r in self.sk.values() if (r.get("gamesPlayed") or 0) >= 3]
        sk.sort(key=lambda r: -(r.get("timeOnIcePerGame") or 0) * (r.get("gamesPlayed") or 0))
        sk = sk[:400]
        go = sorted([r for r in self.go.values() if (r.get("gamesStarted") or 0) >= 2],
                    key=lambda r: -(r.get("gamesStarted") or 0))[:70]
        self.scale = {}
        for c in self.cats:
            grp, field, _ = CATS[c]
            if grp == "S":
                rates = [(r.get(field) or 0) / r["gamesPlayed"] for r in sk]
                m, s = mean(rates), stdev(rates)
                # divide by the spread so every category counts equally (z-score style)
                self.scale[c] = ("sd", 0, s or m or 1)
            elif grp == "G":
                rates = [(r.get(field) or 0) / r["gamesStarted"] for r in go]
                self.scale[c] = ("sd", 0, stdev(rates) or mean(rates) or 1)
            else:
                vals = [r.get(field) or 0 for r in go]
                self.scale[c] = ("ratio", mean(vals), stdev(vals) or 1)

    def per_game(self, row, is_goalie):
        """Fantasy value of one game (skater) / one start (goalie) from totals row."""
        if not row:
            return None
        n = row.get("gamesStarted") if is_goalie else row.get("gamesPlayed")
        if not n:
            return None
        if self.points_league:
            v = 0.0
            for c, mod in self.mods.items():
                grp, field, _ = CATS[c]
                if (grp == "G") == is_goalie and grp != "GR":
                    v += (row.get(field) or 0) / n * mod
            return v
        v = 0.0
        for c in self.cats:
            grp, field, sign = CATS[c]
            if (grp in ("G", "GR")) != is_goalie:
                continue
            kind, mu, sc = self.scale[c]
            if kind == "ratio":
                v += sign * ((row.get(field) or 0) - mu) / sc
            else:
                v += sign * (row.get(field) or 0) / n / sc
        return v

    def goalie_share(self, nhl_id):
        g = self.go.get(nhl_id) or {}
        team = team_abbrev(g)
        tot = sum(r.get("gamesStarted") or 0 for r in self.go.values() if team_abbrev(r) == team)
        return (g.get("gamesStarted") or 0) / tot if tot else 0.5

    def games_for(self, p, nhl_id, counts):
        if p["status"] in OUT_STATUS:
            return 0.0
        row = (self.go if p["is_goalie"] else self.sk).get(nhl_id) or {}
        g = counts.get(team_abbrev(row) or TEAM_FIX.get(p["yteam"], p["yteam"]), 0)
        if p["is_goalie"]:
            g *= self.goalie_share(nhl_id)
        if p["status"] == "DTD":
            g *= 0.7
        return g

    def tracker_preds(self, players, start, end, live=False, market=None):
        """Per-player tracker predictions for a week window. When live, uses all
        stats to date and only the games remaining; otherwise strictly pre-week."""
        today = dt.date.today().isoformat()
        cut = (dt.date.fromisoformat(min(today, end) if live else start) - dt.timedelta(days=1)).isoformat()
        if cut < self.season_start:
            return {}
        season_sk, season_go = nhl_stats(self.season_id, self.season_start, cut)
        r0 = max(self.season_start, (dt.date.fromisoformat(cut) - dt.timedelta(days=13)).isoformat())
        rec_sk, rec_go = nhl_stats(self.season_id, r0, cut)
        counts = team_games(max(start, today) if live else start, end)
        out = {}
        for p in players:
            nid = p.get("nhl_id")
            if not nid:
                continue
            g = self.games_for(p, nid, counts)
            S = season_go if p["is_goalie"] else season_sk
            R = rec_go if p["is_goalie"] else rec_sk
            pg_s = self.per_game(S.get(nid), p["is_goalie"])
            rrow = R.get(nid)
            enough = rrow and (rrow.get("gamesStarted" if p["is_goalie"] else "gamesPlayed") or 0) >= 2
            pg_r = self.per_game(rrow, p["is_goalie"]) if enough else None
            out[p["key"]] = {"season": None if pg_s is None else round(pg_s * g, 3),
                             "recent": None if pg_r is None else round(pg_r * g, 3),
                             "_pg": pg_s, "_games": round(g, 2)}
        if market:
            ref = [v["_pg"] for v in out.values() if v.get("_pg")]
            mapped = quantile_map({k: v for k, v in market.items() if k in out}, ref)
            for k, pg in mapped.items():
                out[k]["market"] = round(pg * out[k]["_games"], 3)
        return out

    def percent_owned(self, keys):
        res = {}
        keys = list(keys)
        for i in range(0, len(keys), 25):
            chunk = ",".join(keys[i:i + 25])
            try:
                fc = yget("league/%s/players;player_keys=%s/percent_owned" % (self.key, chunk), ttl=3600)
            except ApiError:
                continue
            for p in ycoll(fc["league"][1]["players"], "player"):
                pm = yobj(p)
                po = ymerge(pm.get("percent_owned"))
                try:
                    res[pm["player_key"]] = float(po.get("value", 0))
                except (TypeError, ValueError):
                    pass
        return res

    def free_agents(self, n=100):
        out = []
        for start in range(0, n, 25):
            fc = yget("league/%s/players;status=A;sort=AR;count=25;start=%d" % (self.key, start), ttl=1800)
            for p in ycoll(fc["league"][1].get("players"), "player"):
                pm = yobj(p)
                elig = [e["position"] for e in pm.get("eligible_positions", []) if isinstance(e, dict)]
                out.append({"key": pm["player_key"], "id": pm["player_key"], "name": pm["name"]["full"],
                            "yteam": (pm.get("editorial_team_abbr") or "").upper(),
                            "pos": elig, "display_pos": pm.get("display_position"),
                            "is_goalie": pm.get("position_type") == "G", "status": pm.get("status"),
                            "slot": None})
        return out

    @staticmethod
    def eligible(slot, pos):
        return slot in pos

    # --------------------------------------------------------- dashboard
    def dashboard(self):
        start, end = self.weeks.get(self.week, (dt.date.today().isoformat(),) * 2)
        weights, wdetail = fb.learned_weights("nhl", self.key)
        allp = [p for t in self.teams.values() for p in t["players"]]
        fas = self.free_agents()
        for p in allp + fas:
            p["nhl_id"] = self.match(p)
        market = self.percent_owned([p["key"] for p in allp + fas])
        live = self.tracker_preds(allp + fas, start, end, live=True, market=market)
        pre = self.tracker_preds(allp + fas, start, end, live=False, market=market)
        fb.log_predictions("nhl", self.key, self.week,
                           {k: {s: v for s, v in d.items() if not s.startswith("_")} for k, d in pre.items()},
                           replace_sources=("season", "recent"))
        for p in allp + fas:
            src = live.get(p["key"], {})
            p["trackers"] = {s: src.get(s) for s in SOURCES}
            p["games_left"] = src.get("_games", 0)
            p["value"] = round(blend(p["trackers"], weights), 3)
            pg = src.get("_pg") or 0
            mk = (src.get("market") / src["_games"]) if src.get("market") and src.get("_games") else None
            rc = (src.get("recent") / src["_games"]) if src.get("recent") and src.get("_games") else None
            per = blend({"season": pg or None, "recent": rc, "market": mk}, weights)
            share = self.goalie_share(p["nhl_id"]) if p["is_goalie"] and p["nhl_id"] else 1
            p["ros"] = round(per * 3.5 * share, 3)  # value of an average week
            p["owned"] = market.get(p["key"])
        week_vals, ros_vals, per_src = {}, {}, {s: {} for s in SOURCES}
        for tk, t in self.teams.items():
            act = [p for p in t["players"] if p["slot"] not in ("IR", "IR+", "IRLT", "NA")]
            week_vals[tk], _ = optimize_lineup(act, self.slots, self.eligible, "value")
            ros_vals[tk], _ = optimize_lineup(act, self.slots, self.eligible, "ros")
            for s in SOURCES:
                tmp = [dict(p, v=p["trackers"].get(s) or 0) for p in act]
                per_src[s][tk], _ = optimize_lineup(tmp, self.slots, self.eligible, "v")
        g_week, g_ros = grade_teams(week_vals), grade_teams(ros_vals)
        g_src = {s: grade_teams(v) for s, v in per_src.items() if any(v.values())}
        cat_grades = self.category_grades()
        teams = []
        for tk, t in self.teams.items():
            teams.append({"id": tk, "name": t["name"], "mine": t["mine"], "manager": t["manager"],
                          "week": g_week[tk], "ros": g_ros[tk],
                          "trackers": {s: g[tk] for s, g in g_src.items()},
                          "positions": {c: g[tk]["grade"] for c, g in cat_grades.items()},
                          "players": sorted([self._pub(p) for p in t["players"]], key=lambda p: -p["ros"])})
        self._standings(teams)
        teams.sort(key=lambda t: t["ros"]["rank"])
        games = self.matchups(start, end)
        advice = self.advice(fas, start, end)
        trades = []
        if self.my_team:
            tp = {tk: [p for p in t["players"] if p["slot"] not in ("IR", "IR+", "IRLT", "NA")]
                  for tk, t in self.teams.items()}
            for tr in trade_finder(self.my_team, tp, self.slots, self.eligible, "ros"):
                tr["give"], tr["get"] = self._pub(tr["give"]), self._pub(tr["get"])
                tr["team"] = self.teams[tr["team_id"]]["name"]
                trades.append(tr)
        return {"sport": "nhl", "league": {"id": self.key, "name": self.meta["name"], "season": self.season,
                                            "week": self.week, "slots": self.slots, "cats": self.cats,
                                            "points": self.points_league, "window": [start, end]},
                "weights": weights, "weight_detail": wdetail, "teams": teams, "games": games,
                "advice": advice, "trades": trades, "trending": []}

    def _pub(self, p):
        return {"id": p["key"], "name": p["name"], "pos": [x for x in p["pos"] if x not in ("Util", "BN", "IR", "IR+", "F")]
                or p["pos"], "team": p["yteam"], "injury": p["status"], "slot": p.get("slot"),
                "starter": p.get("slot") not in (None, "BN", "IR", "IR+", "IRLT", "NA"),
                "trackers": {k: (None if v is None else round(v, 2)) for k, v in p.get("trackers", {}).items()},
                "value": round(p.get("value") or 0, 2), "ros": round(p.get("ros") or 0, 2),
                "games_left": p.get("games_left"), "owned": p.get("owned")}

    def _standings(self, teams):
        try:
            fc = yget("league/%s/standings" % self.key, ttl=900)
            rec = {}
            for t in ycoll(fc["league"][1]["standings"][0]["teams"], "team"):
                tm = yobj(t)
                ot = (tm.get("team_standings") or {}).get("outcome_totals") or {}
                rec[tm["team_key"]] = "%s-%s-%s" % (ot.get("wins", 0), ot.get("losses", 0), ot.get("ties", 0))
            for t in teams:
                t["record"] = rec.get(t["id"], "")
        except (ApiError, KeyError, IndexError, TypeError):
            pass

    def category_grades(self):
        """Per-category team strength from rostered players' season rates."""
        out = {}
        for c in (self.cats if not self.points_league else []):
            grp, field, sign = CATS[c]
            vals = {}
            for tk, t in self.teams.items():
                tot = wsum = 0.0
                for p in t["players"]:
                    if (grp != "S") != p["is_goalie"] or not p.get("nhl_id"):
                        continue
                    row = (self.go if p["is_goalie"] else self.sk).get(p["nhl_id"]) or {}
                    if grp == "GR":
                        tot += (row.get(field) or 0) * (row.get("gamesStarted") or 0)
                        wsum += row.get("gamesStarted") or 0
                    else:
                        n = row.get("gamesStarted" if p["is_goalie"] else "gamesPlayed") or 0
                        tot += (row.get(field) or 0) / n if n else 0
                vals[tk] = sign * (tot / wsum if grp == "GR" and wsum else tot)
            out[c] = grade_teams(vals)
        return out

    # ---------------------------------------------------------- matchups
    def _team_stats(self, team_obj):
        out = {}
        for s in ((team_obj.get("team_stats") or {}).get("stats") or []):
            s = s["stat"]
            try:
                out[self.stat_names.get(str(s["stat_id"]))] = float(s["value"])
            except (TypeError, ValueError):
                pass
        return out

    def _scoreboard(self, week, ttl=300):
        fc = yget("league/%s/scoreboard;week=%d" % (self.key, week), ttl=ttl)
        sb = fc["league"][1]["scoreboard"]
        ms = ycoll(sb.get("0", {}).get("matchups") if "0" in sb else sb.get("matchups"), "matchup")
        out = []
        for m in ms:
            ts = [yobj(t) for t in ycoll(m.get("0", {}).get("teams"), "team")]
            out.append({"m": m, "teams": ts})
        return out

    def matchups(self, start, end):
        today = dt.date.today().isoformat()
        counts = team_games(max(start, today), end)
        games = []
        try:
            board = self._scoreboard(self.week)
        except (ApiError, KeyError, IndexError, TypeError):
            return games
        for mu in board:
            if len(mu["teams"]) != 2:
                continue
            a, b = mu["teams"]
            proj = []
            for t in (a, b):
                cur = self._team_stats(t)
                try:
                    cur["_pts"] = float((t.get("team_points") or {}).get("total") or 0)
                except (TypeError, ValueError):
                    cur["_pts"] = 0.0
                proj.append(self._proj_team(t["team_key"], counts, cur))
            prob, exp_cats = self._simulate(proj[0], proj[1])
            fb.log_matchup("nhl", self.key, self.week, a["team_key"], b["team_key"], round(prob, 3))
            fb.log_matchup("nhl", self.key, self.week, b["team_key"], a["team_key"], round(1 - prob, 3))
            games.append({"a": a["name"], "b": b["name"], "a_win": round(prob, 3),
                          "mine": self.my_team in (a["team_key"], b["team_key"]),
                          "a_proj": round(proj[0].get("_pts", exp_cats), 1),
                          "b_proj": round(proj[1].get("_pts", len(self.cats) - exp_cats), 1),
                          "cats": [{"cat": c, "a": round(proj[0].get(c, 0), 3), "b": round(proj[1].get(c, 0), 3)}
                                   for c in self.cats]})
        return games

    def _proj_team(self, tk, counts, current):
        t = self.teams.get(tk)
        if not t:
            return {}
        act = [p for p in t["players"] if p["slot"] not in ("IR", "IR+", "IRLT", "NA")]
        _, lineup = optimize_lineup(act, self.slots, self.eligible, "value")
        tot = {c: (current.get(c) or 0) for c in self.cats if CATS[c][0] != "GR"}
        ga = sa = sv = starts = 0.0
        pts = current.get("_pts", 0)
        for _, p in lineup:
            if not p or not p.get("nhl_id"):
                continue
            row = (self.go if p["is_goalie"] else self.sk).get(p["nhl_id"]) or {}
            n = row.get("gamesStarted" if p["is_goalie"] else "gamesPlayed") or 0
            g = self.games_for(p, p["nhl_id"], counts)
            if not n:
                continue
            for c in self.cats:
                grp, field, _ = CATS[c]
                if grp == "S" and not p["is_goalie"] or grp == "G" and p["is_goalie"]:
                    tot[c] = tot.get(c, 0) + (row.get(field) or 0) / n * g
            if p["is_goalie"]:
                ga += (row.get("goalsAgainst") or 0) / n * g
                sa += (row.get("shotsAgainst") or 0) / n * g
                sv += (row.get("saves") or 0) / n * g
                starts += g
            pg = self.per_game(row, p["is_goalie"]) if self.points_league else 0
            pts += (pg or 0) * g
        if "GAA" in self.cats:
            tot["GAA"] = current.get("GAA") if not starts else ga / starts
        if "SV%" in self.cats:
            tot["SV%"] = current.get("SV%") if not sa else sv / sa
        if self.points_league:
            tot["_pts"] = pts
        return tot

    def _simulate(self, A, B, n=3000):
        if self.points_league:
            pa, pb = A.get("_pts", 0), B.get("_pts", 0)
            from .core import norm_cdf
            return norm_cdf((pa - pb) / ((0.25 * (pa + pb) / 2) * 1.414 or 1)), 0
        rnd = random.Random(7)
        wins = exp = 0.0
        for _ in range(n):
            wa = wb = 0
            for c in self.cats:
                grp, _, sign = CATS[c]
                a, b = A.get(c) or 0, B.get(c) or 0
                if grp == "GR":
                    sd = 0.35 if c == "GAA" else 0.008
                else:
                    sd = max(1.0, (abs(a) + abs(b)) ** 0.5) * 0.75
                da = rnd.gauss(a, sd) - rnd.gauss(b, sd)
                if sign * da > 0:
                    wa += 1
                elif sign * da < 0:
                    wb += 1
            exp += wa
            wins += 1 if wa > wb else 0.5 if wa == wb else 0
        return wins / n, exp / n

    # ------------------------------------------------------------ advice
    def advice(self, fas, start, end):
        out = {"start_sit": [], "waivers": []}
        if not self.my_team:
            return out
        mine = self.teams[self.my_team]["players"]
        today = dt.date.today().isoformat()
        playing = team_games(today, today)
        # today's lineup check
        def plays(p):
            row = (self.go if p["is_goalie"] else self.sk).get(p.get("nhl_id")) or {}
            return playing.get(team_abbrev(row) or TEAM_FIX.get(p["yteam"], p["yteam"]), 0) > 0
        benched = [p for p in mine if p["slot"] == "BN" and plays(p) and p["status"] not in OUT_STATUS]
        idle = [p for p in mine if p["slot"] not in NON_STARTING and p["slot"] and not plays(p)]
        for b in sorted(benched, key=lambda p: -(p.get("ros") or 0)):
            swap = next((s for s in idle if any(x in b["pos"] for x in [s["slot"]])), None)
            if swap:
                idle.remove(swap)
                out["start_sit"].append({"start": b["name"], "sit": swap["name"], "edge": round(b["ros"] / 3.5, 2),
                                         "text": "Today: start %s (has a game) in %s's %s slot (no game)"
                                         % (b["name"], swap["name"], swap["slot"])})
            else:
                out["start_sit"].append({"start": b["name"], "sit": "", "edge": round(b["ros"] / 3.5, 2),
                                         "text": "Today: %s has a game but is on your bench" % b["name"]})
        # waivers: best available vs your weakest at a shared position, this week + ROS
        cands = []
        for f in fas:
            if f["status"] in OUT_STATUS or not f.get("nhl_id"):
                continue
            same = [p for p in mine if set(p["pos"]) & set(f["pos"]) - {"Util", "BN"}
                    and p["slot"] not in ("IR", "IR+", "IRLT", "NA")]
            worst = min(same, key=lambda p: p.get("ros") or 0) if same else None
            gain = (f.get("ros") or 0) - (worst.get("ros") or 0 if worst else 0)
            week_gain = (f.get("value") or 0) - (worst.get("value") or 0 if worst else 0)
            if gain > 0.15:  # must be better for the rest of the season, not just a hot week
                cands.append((gain + 0.3 * max(week_gain, 0), f, worst, gain, week_gain))
        cands.sort(key=lambda x: -x[0])
        for score, f, worst, gain, wg in cands[:12]:
            txt = "Add %s (%s, %s games left this week)%s" % (
                f["name"], f.get("display_pos"), f.get("games_left"),
                " - drop %s" % worst["name"] if worst else "")
            out["waivers"].append({"add": self._pub(f), "drop": worst and worst["name"],
                                   "ros": f.get("ros"), "gain": round(gain, 2), "week_gain": round(wg, 2),
                                   "text": txt})
            fb.log_advice("nhl", self.key, self.week, "add", txt, f["key"], worst["key"] if worst else "",
                          round(score, 2))
        return out

    # --------------------------------------------------- feedback update
    def run_feedback(self):
        allp = [p for t in self.teams.values() for p in t["players"]]
        try:
            self._fas = self.free_agents()
        except ApiError:
            self._fas = []
        for p in allp + self._fas:
            p["nhl_id"] = self.match(p)
        done = []
        for w in sorted(self.weeks):
            if w >= self.week:
                break
            start, end = self.weeks[w]
            if end >= dt.date.today().isoformat():
                continue
            preds = self.tracker_preds(allp, start, end)
            if not preds:
                continue
            fb.log_predictions("nhl", self.key, w,
                               {k: {s: v for s, v in d.items() if not s.startswith("_")} for k, d in preds.items()},
                               replace_sources=("season", "recent"))
            wk_sk, wk_go = nhl_stats(self.season_id, start, end)
            actual = {}
            for p in allp:
                nid = p.get("nhl_id")
                row = (wk_go if p["is_goalie"] else wk_sk).get(nid) if nid else None
                if p["key"] not in preds:
                    continue
                if not row:
                    actual[p["key"]] = 0.0
                    continue
                n = row.get("gamesStarted" if p["is_goalie"] else "gamesPlayed") or 0
                actual[p["key"]] = round((self.per_game(row, p["is_goalie"]) or 0) * n, 3)
            fb.log_actuals("nhl", self.key, w, actual)
            fb.score_week("nhl", self.key, w)
            # matchup results for anything we predicted live
            try:
                for mu in self._scoreboard(w, ttl=PAST):
                    m = mu["m"]
                    win = m.get("winner_team_key")
                    tied = str(m.get("is_tied", "0")) == "1"
                    for t in mu["teams"]:
                        res = 0.5 if tied else (1.0 if t["team_key"] == win else 0.0)
                        if win or tied:
                            fb.set_matchup_result("nhl", self.key, w, t["team_key"], res)
            except (ApiError, KeyError, IndexError, TypeError):
                pass
            my_keys = {p["key"] for p in self.teams[self.my_team]["players"]} if self.my_team else set()
            fb.resolve_advice("nhl", self.key, w,
                              lambda r: int(r["player_a"] in my_keys),
                              lambda r: self._advice_outcome(r, wk_sk, wk_go))
            done.append(w)
        return {"weeks_scored": done}

    def _advice_outcome(self, r, wk_sk, wk_go):
        def val(key):
            pool = [x for t in self.teams.values() for x in t["players"]] + getattr(self, "_fas", [])
            p = next((x for x in pool if x["key"] == key), None)
            if not p or not p.get("nhl_id"):
                return 0.0
            row = (wk_go if p["is_goalie"] else wk_sk).get(p["nhl_id"])
            if not row:
                return 0.0
            n = row.get("gamesStarted" if p["is_goalie"] else "gamesPlayed") or 0
            return (self.per_game(row, p["is_goalie"]) or 0) * n
        return round(val(r["player_a"]) - (val(r["player_b"]) if r["player_b"] else 0), 2)

"""Shared plumbing: HTTP + disk cache, config, SQLite, math helpers,
lineup optimizer, grading and trade finder. Python 3.9 stdlib only."""
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "cache")
os.makedirs(CACHE, exist_ok=True)
UA = "FantasyEdge/1.0 (personal fantasy assistant)"


class ApiError(Exception):
    pass


# --------------------------------------------------------------------------- HTTP
def http_json(url, ttl=600, headers=None, data=None, method=None):
    """GET/POST JSON. GETs are cached on disk for `ttl` seconds; if the network
    fails we fall back to any stale cached copy."""
    path = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".json")
    cacheable = data is None and ttl
    if cacheable and os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        with open(path) as f:
            return json.load(f)
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf8", "replace")
        if cacheable and os.path.exists(path) and e.code >= 500:
            with open(path) as f:
                return json.load(f)
        raise ApiError("HTTP %s from %s: %s" % (e.code, url.split("?")[0], detail))
    except (urllib.error.URLError, TimeoutError) as e:
        if cacheable and os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        raise ApiError("Network error for %s: %s" % (url.split("?")[0], e))
    out = json.loads(body) if body else None
    if cacheable:
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())  # unique per thread
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, path)
    return out


# ------------------------------------------------------------------------- config
_CFG = os.path.join(DATA, "config.json")
_cfg_lock = threading.Lock()


def load_config():
    if not os.path.exists(_CFG):
        return {}
    with open(_CFG) as f:
        return json.load(f)


def save_config(cfg):
    with _cfg_lock:
        tmp = _CFG + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _CFG)


def update_config(**kw):
    cfg = load_config()
    cfg.update(kw)
    save_config(cfg)
    return cfg


# ----------------------------------------------------------------------- database
_DB = os.path.join(DATA, "edge.db")
_db_lock = threading.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions(sport TEXT, league TEXT, week INTEGER, player_id TEXT,
  source TEXT, value REAL, created REAL, PRIMARY KEY(sport, league, week, player_id, source));
CREATE TABLE IF NOT EXISTS actuals(sport TEXT, league TEXT, week INTEGER, player_id TEXT,
  value REAL, PRIMARY KEY(sport, league, week, player_id));
CREATE TABLE IF NOT EXISTS source_scores(sport TEXT, league TEXT, week INTEGER, source TEXT,
  spearman REAL, n INTEGER, PRIMARY KEY(sport, league, week, source));
CREATE TABLE IF NOT EXISTS matchup_preds(sport TEXT, league TEXT, week INTEGER, team_id TEXT,
  opp_id TEXT, win_prob REAL, result REAL, created REAL, PRIMARY KEY(sport, league, week, team_id));
CREATE TABLE IF NOT EXISTS advice(id INTEGER PRIMARY KEY AUTOINCREMENT, sport TEXT, league TEXT,
  week INTEGER, kind TEXT, text TEXT, player_a TEXT, player_b TEXT, edge REAL, created REAL,
  followed INTEGER, outcome REAL, UNIQUE(sport, league, week, kind, player_a, player_b));
CREATE TABLE IF NOT EXISTS lineup_eff(sport TEXT, league TEXT, week INTEGER, team_id TEXT,
  team_name TEXT, actual REAL, optimal REAL, PRIMARY KEY(sport, league, week, team_id));
"""


def db():
    con = sqlite3.connect(_DB, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def db_write(sql, rows):
    with _db_lock:
        con = db()
        try:
            con.executemany(sql, rows)
            con.commit()
        finally:
            con.close()


def db_query(sql, args=()):
    con = db()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


# --------------------------------------------------------------------------- math
def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s)
    return re.sub(r"[^a-z]", "", s)


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0
        i = j + 1
    return r


def pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def spearman(a, b):
    return pearson(ranks(a), ranks(b))


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def quantile_map(src, ref):
    """Map each value in dict `src` onto the distribution of `ref` values by rank.
    Used to turn 'market' numbers (trade value, % owned) into points-scale."""
    if not src or not ref:
        return {}
    keys = sorted(src, key=lambda k: -src[k])
    refs = sorted(ref, reverse=True)
    out = {}
    for i, k in enumerate(keys):
        j = min(int(i * len(refs) / len(keys)), len(refs) - 1)
        out[k] = refs[j]
    return out


GRADE_CUTS = [(1.5, "A+"), (1.0, "A"), (0.6, "A-"), (0.3, "B+"), (0.0, "B"), (-0.3, "B-"),
              (-0.6, "C+"), (-1.0, "C"), (-1.4, "D")]


def letter(z):
    for cut, g in GRADE_CUTS:
        if z >= cut:
            return g
    return "F"


def grade_teams(values):
    """{team_id: number} -> {team_id: {"value", "z", "grade", "rank"}}"""
    vals = list(values.values())
    m, s = mean(vals), stdev(vals) or 1.0
    order = sorted(values, key=lambda k: -values[k])
    return {k: {"value": round(values[k], 2), "z": round((values[k] - m) / s, 2),
                "grade": letter((values[k] - m) / s), "rank": order.index(k) + 1} for k in values}


# --------------------------------------------------------------- lineup optimizer
def optimize_lineup(players, slots, eligible, key="value"):
    """Greedy, most-restrictive-slot-first assignment.
    players: list of dicts with 'id', 'pos' (list) and `key`.
    slots: list of slot names (bench/IR already removed).
    eligible(slot, pos_list) -> bool.
    Returns (total, [(slot, player_or_None)])."""
    pool = sorted(players, key=lambda p: -(p.get(key) or 0))
    order = sorted(range(len(slots)),
                   key=lambda i: sum(1 for p in pool if eligible(slots[i], p["pos"])))
    used, result, total = set(), [None] * len(slots), 0.0
    for i in order:
        for p in pool:
            if p["id"] not in used and eligible(slots[i], p["pos"]):
                used.add(p["id"])
                result[i] = p
                total += p.get(key) or 0
                break
    return total, list(zip(slots, result))


def roster_strength(players, slots, eligible, key="value", depth=0.15):
    """Starter total plus a small credit for the best bench players (depth)."""
    total, lineup = optimize_lineup(players, slots, eligible, key)
    started = {p["id"] for _, p in lineup if p}
    bench = sorted((p.get(key) or 0 for p in players if p["id"] not in started), reverse=True)
    return total + depth * sum(bench[:4])


def trade_finder(my_id, teams, slots, eligible, key="value", limit=12):
    """1-for-1 trades that improve BOTH lineups. teams: {team_id: [players]}"""
    mine = teams[my_id]
    base_me = roster_strength(mine, slots, eligible, key)
    ideas = []
    for tid, theirs in teams.items():
        if tid == my_id:
            continue
        base_them = roster_strength(theirs, slots, eligible, key)
        for a in mine:
            for b in theirs:
                if (a.get(key) or 0) <= 0 and (b.get(key) or 0) <= 0:
                    continue
                new_me = [p for p in mine if p["id"] != a["id"]] + [b]
                new_them = [p for p in theirs if p["id"] != b["id"]] + [a]
                d_me = roster_strength(new_me, slots, eligible, key) - base_me
                d_them = roster_strength(new_them, slots, eligible, key) - base_them
                if d_me > 0.05 and d_them > 0.05:
                    ideas.append({"team_id": tid, "give": a, "get": b,
                                  "you_gain": round(d_me, 2), "they_gain": round(d_them, 2)})
    ideas.sort(key=lambda t: -(min(t["you_gain"], t["they_gain"] * 1.5)))
    seen, out = set(), []
    for t in ideas:
        k = (t["give"]["id"], t["get"]["id"])
        if k not in seen:
            seen.add(k)
            out.append(t)
        if len(out) >= limit:
            break
    return out


def blend(preds, weights):
    """Weighted average of the sources that have a prediction for this player."""
    num = den = 0.0
    for s, v in preds.items():
        if v is None:
            continue
        w = weights.get(s, 0)
        num += w * v
        den += w
    return num / den if den else 0.0

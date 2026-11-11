"""The feedback loops.

Loop 1  Source accuracy -> weights.  Every tracker's per-player prediction for a
        week is logged. When the week finishes we score each tracker against what
        actually happened (Spearman rank correlation) and re-weight the trackers:
        better recent accuracy = more say in next week's grades.
Loop 2  Matchup calibration.  Win probabilities are logged before the week and
        scored (Brier score + hit rate) after it.
Loop 3  Advice outcomes.  Start/sit and add/drop calls are logged, the app detects
        whether you followed them, and records how many points the call was worth.
Loop 4  Lineup efficiency.  Actual lineup vs. the best lineup you could have set,
        for every team in the league, each week.
"""
import time

from .core import db_query, db_write, spearman

PRIOR = {
    "nfl": {"proj": 0.45, "season": 0.2, "recent": 0.15, "market": 0.2},
    "nhl": {"season": 0.45, "recent": 0.25, "market": 0.3},
}
HALF_LIFE_WEEKS = 3.0
PRIOR_STRENGTH = 2.0  # how many weeks of evidence the prior is worth


def log_predictions(sport, league, week, preds, replace_sources=()):
    """preds: {player_id: {source: value}}. Sources in replace_sources are
    reconstructible from history (overwrite); others are first-snapshot-wins."""
    now = time.time()
    rep, ign = [], []
    for pid, src in preds.items():
        for s, v in src.items():
            if v is None:
                continue
            row = (sport, league, week, str(pid), s, float(v), now)
            (rep if s in replace_sources else ign).append(row)
    if rep:
        db_write("INSERT OR REPLACE INTO predictions VALUES (?,?,?,?,?,?,?)", rep)
    if ign:
        db_write("INSERT OR IGNORE INTO predictions VALUES (?,?,?,?,?,?,?)", ign)


def log_actuals(sport, league, week, actual):
    db_write("INSERT OR REPLACE INTO actuals VALUES (?,?,?,?,?)",
             [(sport, league, week, str(p), float(v)) for p, v in actual.items()])


def score_week(sport, league, week):
    """Loop 1: grade every tracker for a finished week."""
    act = {r["player_id"]: r["value"] for r in db_query(
        "SELECT player_id, value FROM actuals WHERE sport=? AND league=? AND week=?",
        (sport, league, week))}
    if not act:
        return {}
    by_src = {}
    for r in db_query("SELECT player_id, source, value FROM predictions WHERE sport=? AND league=? AND week=?",
                      (sport, league, week)):
        if r["player_id"] in act:
            by_src.setdefault(r["source"], []).append((r["value"], act[r["player_id"]]))
    out, rows = {}, []
    for s, pairs in by_src.items():
        if len(pairs) < 8:
            continue
        rho = spearman([p for p, _ in pairs], [a for _, a in pairs])
        if rho is None:
            continue
        out[s] = rho
        rows.append((sport, league, week, s, rho, len(pairs)))
    db_write("INSERT OR REPLACE INTO source_scores VALUES (?,?,?,?,?,?)", rows)
    return out


def learned_weights(sport, league):
    """Recency-weighted accuracy per tracker, blended with a prior, squared to
    reward the clearly-better trackers, then normalized."""
    prior = PRIOR[sport]
    rows = db_query("SELECT week, source, spearman, n FROM source_scores WHERE sport=? AND league=?",
                    (sport, league))
    latest = max([r["week"] for r in rows] or [0])
    acc, detail = {}, {}
    for s in prior:
        rs = [r for r in rows if r["source"] == s]
        wsum = sum(0.5 ** ((latest - r["week"]) / HALF_LIFE_WEEKS) for r in rs)
        if wsum:
            rho = sum(r["spearman"] * 0.5 ** ((latest - r["week"]) / HALF_LIFE_WEEKS) for r in rs) / wsum
        else:
            rho = None
        detail[s] = {"weeks_scored": len(rs), "recent_accuracy": None if rho is None else round(rho, 3)}
        evidence = max(rho, 0.02) ** 2 if rho is not None else None
        acc[s] = (evidence, len(rs))
    # normalize evidence so it's comparable to the prior's scale
    ev_tot = sum(e for e, _ in acc.values() if e is not None) or 1.0
    raw = {}
    for s, (e, k) in acc.items():
        k_eff = min(k, 8)
        if e is None:
            raw[s] = prior[s]
        else:
            raw[s] = (k_eff * (e / ev_tot) + PRIOR_STRENGTH * prior[s]) / (k_eff + PRIOR_STRENGTH)
    tot = sum(raw.values()) or 1.0
    weights = {s: round(v / tot, 3) for s, v in raw.items()}
    for s in detail:
        detail[s]["weight"] = weights[s]
        detail[s]["prior"] = prior[s]
    return weights, detail


def log_matchup(sport, league, week, team_id, opp_id, win_prob, result=None, keep_first=True):
    sql = ("INSERT OR IGNORE" if keep_first else "INSERT OR REPLACE") + \
        " INTO matchup_preds VALUES (?,?,?,?,?,?,?,?)"
    db_write(sql, [(sport, league, week, str(team_id), str(opp_id), win_prob, result, time.time())])


def set_matchup_result(sport, league, week, team_id, result):
    db_write("UPDATE matchup_preds SET result=? WHERE sport=? AND league=? AND week=? AND team_id=?",
             [(result, sport, league, week, str(team_id))])


def log_advice(sport, league, week, kind, text, a, b, edge):
    db_write("INSERT OR IGNORE INTO advice(sport,league,week,kind,text,player_a,player_b,edge,created)"
             " VALUES (?,?,?,?,?,?,?,?,?)", [(sport, league, week, kind, text, str(a), str(b), edge, time.time())])


def resolve_advice(sport, league, week, followed_fn, outcome_fn):
    rows = db_query("SELECT * FROM advice WHERE sport=? AND league=? AND week=? AND outcome IS NULL",
                    (sport, league, week))
    upd = []
    for r in rows:
        oc = outcome_fn(r)
        if oc is None:
            continue
        upd.append((followed_fn(r), oc, r["id"]))
    db_write("UPDATE advice SET followed=?, outcome=? WHERE id=?", upd)


def log_lineup_eff(sport, league, week, rows):
    db_write("INSERT OR REPLACE INTO lineup_eff VALUES (?,?,?,?,?,?,?)",
             [(sport, league, week, str(t), n, a, o) for t, n, a, o in rows])


def report(sport, league, my_team=None):
    weights, detail = learned_weights(sport, league)
    scores = db_query("SELECT week, source, spearman, n FROM source_scores WHERE sport=? AND league=? ORDER BY week",
                      (sport, league))
    mps = db_query("SELECT * FROM matchup_preds WHERE sport=? AND league=? AND result IS NOT NULL",
                   (sport, league))
    brier = sum((m["win_prob"] - m["result"]) ** 2 for m in mps) / len(mps) if mps else None
    called = [m for m in mps if m["result"] != 0.5]
    hit = sum(1 for m in called if (m["win_prob"] >= 0.5) == (m["result"] == 1)) / len(called) if called else None
    advice = db_query("SELECT * FROM advice WHERE sport=? AND league=? ORDER BY week DESC, edge DESC LIMIT 200",
                      (sport, league))
    resolved = [a for a in advice if a["outcome"] is not None]
    adv_sum = {
        "logged": len(advice), "resolved": len(resolved),
        "hit_rate": round(sum(1 for a in resolved if a["outcome"] > 0) / len(resolved), 3) if resolved else None,
        "net_points": round(sum(a["outcome"] for a in resolved), 1) if resolved else None,
        "followed": sum(1 for a in resolved if a["followed"]),
        "ignored_cost": round(sum(a["outcome"] for a in resolved if not a["followed"] and a["outcome"] > 0), 1)
        if resolved else None,
    }
    eff = db_query("SELECT * FROM lineup_eff WHERE sport=? AND league=? ORDER BY week", (sport, league))
    return {"weights": weights, "sources": detail, "source_scores": scores,
            "matchups": {"scored": len(mps), "brier": None if brier is None else round(brier, 3),
                         "hit_rate": None if hit is None else round(hit, 3),
                         "baseline_brier": 0.25},
            "advice_summary": adv_sum, "advice": advice, "lineup_eff": eff, "my_team": my_team}

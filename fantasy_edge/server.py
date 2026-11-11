"""Local web server: python3 -m fantasy_edge.server  ->  http://127.0.0.1:8765"""
import json
import os
import threading
import time
import traceback
import webbrowser
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import feedback as fb
from . import football, hockey
from .core import ApiError, ROOT, load_config, update_config

PORT = int(os.environ.get("FANTASY_EDGE_PORT", "8765"))
_bg = {}


def background_feedback(sport, run, league=""):
    """Feedback loop runs itself: at most every 6h per league, in the background."""
    key = "last_feedback_%s_%s" % (sport, league)
    if time.time() - load_config().get(key, 0) < 6 * 3600 or _bg.get(key):
        return

    def go():
        _bg[key] = True
        try:
            run()
            update_config(**{key: time.time()})
        except Exception:
            traceback.print_exc()
        finally:
            _bg[key] = False
    threading.Thread(target=go, daemon=True).start()


_dash = {}
_dash_lock = threading.Lock()
DASH_TTL = 600


def cached_dashboard(sport, lid, force=False):
    """Dashboards are expensive (trade finder etc.); keep them 10 minutes."""
    key = (sport, lid)
    hit = _dash.get(key)
    if hit and not force and time.time() - hit[0] < DASH_TTL:
        return hit[1]
    lg = nfl_league(lid) if sport == "nfl" else nhl_league()
    out = lg.dashboard()
    with _dash_lock:
        _dash[(sport, out["league"]["id"])] = (time.time(), out)
    background_feedback(sport, lg.run_feedback, out["league"]["id"])
    return out


def league_summary(d):
    """Slim per-league payload for the Home page."""
    sp, lid = d["sport"], d["league"]["id"]
    me = next((t for t in d["teams"] if t["mine"]), None)
    game = next((g for g in d["games"] if g["mine"]), None)
    mg = None
    if game and me:
        a_is_me = game["a"] == me["name"]
        mg = {"opp": game["b"] if a_is_me else game["a"],
              "win": game["a_win"] if a_is_me else 1 - game["a_win"],
              "me_proj": game["a_proj"] if a_is_me else game["b_proj"],
              "opp_proj": game["b_proj"] if a_is_me else game["a_proj"]}
    eff = fb.db_query("SELECT week, team_id, actual, optimal FROM lineup_eff WHERE sport=? AND league=? ORDER BY week",
                      (sp, lid))
    weeks = {}
    for r in eff:
        weeks.setdefault(r["week"], []).append(r)
    my_id = str(me["id"]) if me else None
    history = []
    for w in sorted(weeks):
        rows = weeks[w]
        mine = next((r for r in rows if r["team_id"] == my_id), None)
        history.append({"week": w, "me": mine and round(mine["actual"], 1),
                        "optimal": mine and round(mine["optimal"], 1),
                        "avg": round(sum(r["actual"] for r in rows) / len(rows), 1),
                        "high": round(max(r["actual"] for r in rows), 1)})
    rep = fb.report(sp, lid, my_id)
    return {"sport": sp, "id": lid, "name": d["league"]["name"], "week": d["league"]["week"],
            "me": me and {k: me.get(k) for k in ("name", "record", "week", "ros", "pf", "positions")},
            "matchup": mg,
            "teams": [{"name": t["name"], "mine": t["mine"], "ros": t["ros"], "week": t["week"],
                       "record": t.get("record")} for t in d["teams"]],
            "start_sit": d["advice"]["start_sit"][:4], "waivers": d["advice"]["waivers"][:4],
            "trades": d["trades"][:3],
            "trending": [t for t in d.get("trending", []) if t.get("available")][:5],
            "history": history, "weights": d["weights"],
            "model": {"source_scores": rep["source_scores"], "matchups": rep["matchups"],
                      "advice": rep["advice_summary"]}}


def home(force=False):
    from concurrent.futures import ThreadPoolExecutor
    jobs = [("nfl", l["id"], l["name"]) for l in nfl_leagues()]
    if load_config().get("yahoo_league"):
        jobs.append(("nhl", load_config()["yahoo_league"], load_config().get("yahoo_league_name")))

    def one(job):
        sp, lid, name = job
        try:
            return league_summary(cached_dashboard(sp, lid, force))
        except Exception as e:  # one broken league shouldn't blank the page
            traceback.print_exc()
            return {"sport": sp, "id": lid, "name": name, "error": str(e)}
    with ThreadPoolExecutor(max_workers=4) as ex:
        leagues = list(ex.map(one, jobs))
    return {"leagues": leagues, "yahoo_connected": bool(load_config().get("yahoo_refresh"))}


def nfl_leagues():
    c = load_config()
    ls = c.get("sleeper_leagues")
    if not ls and c.get("sleeper_league"):
        ls = [{"id": c["sleeper_league"], "name": c.get("sleeper_league_name") or "League"}]
    return ls or []


def nfl_league(lid=None):
    c = load_config()
    ls = nfl_leagues()
    if not ls:
        raise ApiError("Pick your Sleeper leagues on the Connect tab.")
    ids = [l["id"] for l in ls]
    lid = lid if lid in ids else ids[0]
    return football.League(lid, c["sleeper_username"])


def nhl_league():
    c = load_config()
    if not c.get("yahoo_league"):
        raise ApiError("Connect Yahoo and pick a league on the Connect tab.")
    return hockey.League(c["yahoo_league"])


def status():
    c = load_config()
    return {"sleeper_username": c.get("sleeper_username"), "sleeper_leagues": nfl_leagues(),
            "yahoo_configured": bool(c.get("yahoo_client_id")), "yahoo_connected": bool(c.get("yahoo_refresh")),
            "yahoo_redirect": c.get("yahoo_redirect", "oob"),
            "yahoo_league": c.get("yahoo_league"), "yahoo_league_name": c.get("yahoo_league_name"),
            "feedback_running": [k for k, v in _bg.items() if v]}


def route(method, path, body, q=None):
    q = q or {}
    if path == "/api/status":
        return status()
    # ---------------- Sleeper
    if path == "/api/sleeper/connect":
        user, leagues = football.user_leagues(body["username"])
        update_config(sleeper_username=body["username"].strip())
        return {"user": user.get("display_name"), "leagues": leagues}
    if path == "/api/sleeper/leagues":
        ls = [{"id": l["id"], "name": l.get("name") or l["id"]} for l in body["leagues"]]
        if not ls:
            raise ApiError("Pick at least one league.")
        update_config(sleeper_leagues=ls, sleeper_league=ls[0]["id"], sleeper_league_name=ls[0]["name"])
        return status()
    if path == "/api/home":
        return home(q.get("force") == "1")
    if path == "/api/nfl/dashboard":
        return cached_dashboard("nfl", q.get("league") or nfl_leagues()[0]["id"] if nfl_leagues() else None,
                                q.get("force") == "1")
    if path == "/api/nfl/feedback":
        lg = nfl_league(q.get("league") or body.get("league"))
        if method == "POST":
            lg.run_feedback()
            update_config(**{"last_feedback_nfl_" + lg.id: time.time()})
        return fb.report("nfl", lg.id, lg.my_roster)
    # ---------------- Yahoo
    if path == "/api/yahoo/config":
        update_config(yahoo_client_id=body["client_id"].strip(), yahoo_client_secret=body["client_secret"].strip(),
                      yahoo_redirect=(body.get("redirect") or "oob").strip())
        return {"auth_url": hockey.yahoo_auth_url()}
    if path == "/api/yahoo/auth_url":
        return {"auth_url": hockey.yahoo_auth_url()}
    if path == "/api/yahoo/code":
        hockey.yahoo_exchange(body["code"])
        return {"leagues": hockey.yahoo_leagues()}
    if path == "/api/yahoo/leagues":
        return {"leagues": hockey.yahoo_leagues()}
    if path == "/api/yahoo/league":
        update_config(yahoo_league=body["id"], yahoo_league_name=body.get("name"))
        return status()
    if path == "/api/nhl/dashboard":
        return cached_dashboard("nhl", load_config().get("yahoo_league"), q.get("force") == "1")
    if path == "/api/nhl/feedback":
        lg = nhl_league()
        if method == "POST":
            lg.run_feedback()
            update_config(**{"last_feedback_nhl_" + lg.key: time.time()})
        return fb.report("nhl", lg.key, lg.my_team)
    if path == "/api/disconnect":
        c = load_config()
        for k in [k for k in c if k.startswith(body.get("which", "x"))]:
            c.pop(k)
        from .core import save_config
        save_config(c)
        return status()
    raise KeyError(path)


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        path, _, qs = self.path.partition("?")
        query = {k: v[0] for k, v in parse_qs(qs).items()}
        if not path.startswith("/api/"):
            with open(os.path.join(ROOT, "static", "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        # only accept requests from our own page
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT):
            return self._send(403, {"error": "forbidden"})
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        try:
            self._send(200, route(method, path, body, query))
        except KeyError as e:
            self._send(404 if str(e).strip("'") == path else 400, {"error": "Missing: %s" % e})
        except ApiError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = "http://127.0.0.1:%d" % PORT
    print("Fantasy Edge running at %s  (Ctrl+C to stop)" % url)
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""pins_flask.py - Pinterest image search as a Flask app (Termux-friendly).

Run in Termux:
    pkg install python
    pip install flask requests
    python pins_flask.py
Then open http://127.0.0.1:5000 in your browser, or call the API:
    curl "http://127.0.0.1:5000/api/search?q=rem+pfp&n=10&size=orig"

Routes:
    /                       web gallery (masonry grid, infinite scroll, viewer)
    /api/search?q=&n=&size=  full search, n can be an int or 'all'; &plain=1 for
                            plain-text URLs; &filter=0 disables junk filtering
    /api/page?q=&size=      next chunk of ~25 results (stateful, for scroll)
    /q/<query>              short URL -> JSON (same options as /api/search)
    /img?q=<query>          redirect to the first original image
    /img/<query>            same, path style
    /rand/<query>           redirect to a random original image

Flow (from captured browser traffic):
    1. GET /               -> anonymous _pinterest_sess + csrftoken cookies
    2. GET /search/pins/   -> pinterest-version header (app version)
    3. GET /resource/BaseSearchResource/get/ with the cookie jar +
       x-requested-with / x-app-version / x-pinterest-appstate headers
    4. JSON: resource_response.data.results[].images.<size>.url
       resource_response.bookmark = cursor for the next page
"""
import json
import logging
import os
import random
import threading
import time

import requests
from flask import Flask, jsonify, redirect, render_template_string, request

BASE = os.environ.get("PINS_BASE", "https://in.pinterest.com")
# host/port for direct `python pins_flask.py` runs; on Render/PaaS the PORT
# env var wins, and gunicorn handles the actual binding (see start command)
HOST = os.environ.get("PINS_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT") or os.environ.get("PINS_PORT") or "5000")
# Set PINS_KEY to require ?key=<secret> (or X-Api-Key header) on all API
# routes once the server is exposed publicly. Leave empty for open access.
API_KEY = os.environ.get("PINS_KEY", "")

UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36")

NAV_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-IN,en;q=0.8",
    "sec-ch-ua": '"Brave";v="119", "Chromium";v="119", "Not?A_Brand";v="24"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
    "sec-gpc": "1",
    "upgrade-insecure-requests": "1",
    "sec-fetch-site": "none",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
}

SIZES = ["orig", "736x", "564x", "474x", "236x"]


class PinsClient:
    """Anonymous Pinterest search client. Session cookies are reused across
    requests; if Pinterest starts gating results, it re-bootstraps once."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.app_version = "6550262"
        self._bootstrapped = False
        self.exhausted = True
        self.filtered_count = 0
        self.last_has_more = False
        self._pages = {}
        self._lock = threading.Lock()

    # -- session bootstrap ----------------------------------------------
    def bootstrap(self):
        self.s.cookies.clear()
        self.s.get(BASE + "/", headers=NAV_HEADERS, timeout=30)
        r = self.s.get(BASE + "/search/pins/",
                       params={"rs": "typed", "q": "test"},
                       headers={**NAV_HEADERS, "Referer": BASE + "/"},
                       timeout=30)
        v = r.headers.get("pinterest-version")
        if v:
            self.app_version = v
        self._bootstrapped = True

    # -- one page of results ----------------------------------------------
    def _fetch_page(self, query, bookmark=""):
        source_url = "/search/pins/?q=" + requests.utils.quote(query) + "&rs=typed"
        options = {
            "query": query, "scope": "pins", "appliedProductFilters": "---",
            "domains": None, "user": None, "seoDrawerEnabled": False,
            "applied_unified_filters": None, "auto_correction_disabled": False,
            "filter_genai": False, "journey_depth": None, "source_id": None,
            "source_module_id": None, "source_url": source_url,
            "static_feed": False, "selected_one_bar_modules": None,
            "query_pin_sigs": None, "page_size": 25, "gated": True,
            "price_max": None, "price_min": None, "query_image_pins": None,
            "request_params": None, "top_pin_ids": None, "article": None,
            "corpus": None, "filters": None, "rs": "typed",
        }
        if bookmark:
            options["bookmarks"] = [bookmark]
        data = {"options": options, "context": {}}

        headers = {
            "accept": "application/json, text/javascript, */*, q=0.01",
            "x-requested-with": "XMLHttpRequest",
            "x-app-version": self.app_version,
            "x-pinterest-appstate": "background",
            "x-pinterest-pws-handler": "www/search/[scope].js",
            "x-pinterest-source-url": source_url,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": BASE + "/",
        }
        bid = self.s.cookies.get("_b")
        if bid:
            headers["x-pinterest-platform-bid"] = bid.replace('"', "")

        r = self.s.get(
            BASE + "/resource/BaseSearchResource/get/",
            params={"source_url": source_url, "data": json.dumps(data),
                    "_": int(time.time() * 1000)},
            headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()

    # -- turn a raw response page into clean results -----------------------
    def _extract(self, resp, size, junk_filter, seen, seen_sig):
        rr = resp.get("resource_response") or {}
        added = []
        for pin in ((rr.get("data") or {}).get("results")) or []:
            pid = pin.get("id")
            if pid is not None and pid in seen:
                continue                    # repeated pin id
            if pid is not None:
                seen.add(pid)
            images = pin.get("images") or {}
            img = (images.get(size) or images.get("orig")
                   or images.get("736x") or images.get("474x")
                   or images.get("236x") or {})
            # junk filter must judge the TRUE (orig) resolution, never the
            # requested thumbnail size -- a 474x474 thumb of a big pfp is not
            # an icon
            full = images.get("orig") or img
            sig = pin.get("image_signature") or img.get("url")
            if sig is not None and sig in seen_sig:
                self.filtered_count += 1
                continue                    # same image re-pinned under new id
            if junk_filter and self._is_junk(full):
                self.filtered_count += 1
                continue                    # logo / icon / banner material
            if sig is not None:
                seen_sig.add(sig)
            added.append({
                "id": pid,
                "url": img.get("url"),
                "orig_url": (images.get("orig") or img).get("url"),
                "width": full.get("width"),
                "height": full.get("height"),
                "pin": (BASE + "/pin/" + str(pid) + "/") if pid else None,
                "description": pin.get("description") or pin.get("grid_title"),
            })
        return added, rr.get("bookmark") or ""

    # -- junk filter: drop Pinterest brand material (logos, icons, banners) --
    @staticmethod
    def _is_junk(img):
        w = img.get("width") or 0
        h = img.get("height") or 0
        if not (w and h):
            return False
        ratio = w / h
        if ratio >= 2.2 or ratio <= 0.45:
            return True                      # banner / strip shapes
        if max(w, h) <= 512 and 0.85 <= ratio <= 1.15:
            return True                      # small square icon / logo
        return False

    # -- public: full search ------------------------------------------------
    def search(self, query, count=25, size="orig", junk_filter=True):
        if size not in SIZES:
            raise ValueError("size must be one of " + ", ".join(SIZES))
        with self._lock:
            if not self._bootstrapped:
                self.bootstrap()
            results = self._search_locked(query, count, size, junk_filter)
            if results and not results[0].get("url"):
                # gated results -> stale/missing session, retry once
                self.bootstrap()
                results = self._search_locked(query, count, size, junk_filter)
        if results and not results[0].get("url"):
            raise RuntimeError("Pinterest returned gated results "
                               "(no image URLs) even after a fresh session")
        return [r for r in results if r.get("url")][:count]

    def _search_locked(self, query, count, size, junk_filter=True):
        out, bookmark, pages = [], "", 0
        seen, seen_sig = set(), set()
        self.filtered_count = 0
        empty_page = False
        # safety cap: max 100 pages (~2500 pins) even for n=all
        max_pages = min(100, (count + 24) // 25 + 3)
        while len(out) < count and pages < max_pages:
            pages += 1
            added, bookmark = self._extract(
                self._fetch_page(query, bookmark), size, junk_filter,
                seen, seen_sig)
            if not added:
                empty_page = True
                break
            out.extend(added)
            if not bookmark or bookmark == "-end-":
                break
        # did Pinterest run out of results, or did we stop early?
        self.exhausted = (not bookmark or bookmark == "-end-" or empty_page)
        return out

    # -- public: stateful next-chunk (for the gallery's infinite scroll) ---
    def more(self, query, size="orig", junk_filter=True):
        """Return the next ~25 results for a query; remembers its position."""
        if size not in SIZES:
            raise ValueError("size must be one of " + ", ".join(SIZES))
        with self._lock:
            if not self._bootstrapped:
                self.bootstrap()
            key = (query, size)
            st = self._pages.get(key)
            if st is None:
                st = {"bookmark": None, "seen": set(), "seen_sig": set(),
                      "queued": []}
                self._pages[key] = st
            self.filtered_count = 0
            if st["queued"]:
                chunk, st["queued"] = st["queued"][:25], st["queued"][25:]
            elif st["bookmark"] == "":
                chunk = []                    # end of results
            else:
                bm = st["bookmark"] or ""
                added, bm2 = self._extract(
                    self._fetch_page(query, bm), size, junk_filter,
                    st["seen"], st["seen_sig"])
                if added and not added[0].get("url"):
                    self.bootstrap()          # gated -> fresh session, retry
                    st["seen"].clear()
                    st["seen_sig"].clear()
                    added, bm2 = self._extract(
                        self._fetch_page(query, bm), size, junk_filter,
                        st["seen"], st["seen_sig"])
                st["bookmark"] = bm2
                chunk, st["queued"] = added[:25], added[25:]
            self.last_has_more = bool(st["queued"]) or bool(
                st["bookmark"] and st["bookmark"] not in ("", "-end-"))
            return chunk


client = PinsClient()
app = Flask(__name__)
DEV = "@techno_telex"


@app.after_request
def _cors(resp):
    """Allow mobile/web apps on other origins to call this API."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "X-Api-Key, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return resp


@app.before_request
def _auth():
    """API key gate: active only when PINS_KEY is set (public deployments)."""
    if not API_KEY or request.path == "/":
        return
    if request.method == "OPTIONS":
        return
    key = request.args.get("key") or request.headers.get("X-Api-Key", "")
    if key != API_KEY:
        return jsonify({"error": "invalid or missing API key "
                                 "(?key=... or X-Api-Key header)"}), 401


def _timed_search(q, n, size, junk_filter=True):
    t0 = time.time()
    results = client.search(q, n, size, junk_filter)
    return results, round((time.time() - t0) * 1000)


def _filter_on():
    """?filter=0 disables the Pinterest-brand junk filter."""
    return request.args.get("filter", "1") not in ("0", "false", "no")


def _parse_n(raw, default=25):
    """n can be an int or 'all' (everything Pinterest returns, cap 2500)."""
    if raw is None or raw == "":
        return default
    if str(raw).lower() == "all":
        return 2500
    return max(1, min(2500, int(raw)))


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "missing ?q= parameter"}), 400
    try:
        n = _parse_n(request.args.get("n"), 25)
    except ValueError:
        return jsonify({"error": "n must be an integer or 'all'"}), 400
    size = request.args.get("size", "orig")
    if size not in SIZES:
        return jsonify({"error": "size must be one of " + ", ".join(SIZES)}), 400
    try:
        results, ms = _timed_search(q, n, size, _filter_on())
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    if request.args.get("plain") in ("1", "true", "yes"):
        return app.response_class("\n".join(r["url"] for r in results) + "\n",
                                  mimetype="text/plain")
    return jsonify({"query": q, "count": len(results), "results": results,
                    "filtered": client.filtered_count,
                    "has_more": not client.exhausted,
                    "time_ms": ms, "developer": DEV})


@app.route("/api/page")
def api_page():
    """Next chunk of ~25 results for the gallery's infinite scroll."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "missing ?q= parameter"}), 400
    size = request.args.get("size", "474x")
    if size not in SIZES:
        return jsonify({"error": "size must be one of " + ", ".join(SIZES)}), 400
    try:
        chunk = client.more(q, size, _filter_on())
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    return jsonify({"query": q, "count": len(chunk), "results": chunk,
                    "has_more": client.last_has_more, "developer": DEV})


def _clean(q):
    # '+' means space (like a query string) and %20 is decoded by Flask already
    return q.replace("+", " ").strip()


@app.route("/q/<path:query>")
def short_search(query):
    q = _clean(query)
    if not q:
        return jsonify({"error": "empty query"}), 400
    try:
        n = _parse_n(request.args.get("n"), 25)
    except ValueError:
        return jsonify({"error": "n must be an integer or 'all'"}), 400
    try:
        results, ms = _timed_search(q, n, "orig", _filter_on())
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    if request.args.get("plain") in ("1", "true", "yes"):
        return app.response_class("\n".join(r["url"] for r in results) + "\n",
                                  mimetype="text/plain")
    return jsonify({"query": q, "count": len(results), "results": results,
                    "filtered": client.filtered_count,
                    "has_more": not client.exhausted,
                    "time_ms": ms, "developer": DEV})


@app.route("/img")
def img_query_string():
    """?q= style: /img?q=rem+pfp -> redirect to the first original image."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "missing ?q= parameter"}), 400
    try:
        results, ms = _timed_search(q, 1, "orig")
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    if not results:
        return jsonify({"error": "no results"}), 404
    resp = redirect(results[0]["url"], code=302)
    resp.headers["X-Search-Time-Ms"] = str(ms)
    return resp


@app.route("/img/<path:query>")
def short_image(query):
    q = _clean(query)
    if not q:
        return jsonify({"error": "empty query"}), 400
    try:
        results, ms = _timed_search(q, 1, "orig")
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    if not results:
        return jsonify({"error": "no results"}), 404
    resp = redirect(results[0]["url"], code=302)
    resp.headers["X-Search-Time-Ms"] = str(ms)
    return resp


@app.route("/rand/<path:query>")
def short_random(query):
    q = _clean(query)
    if not q:
        return jsonify({"error": "empty query"}), 400
    try:
        results, ms = _timed_search(q, 25, "orig")
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    if not results:
        return jsonify({"error": "no results"}), 404
    resp = redirect(random.choice(results)["url"], code=302)
    resp.headers["X-Search-Time-Ms"] = str(ms)
    return resp


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pins · image search</title>
<style>
:root{--bg:#0e0f13;--bar:#16181f;--card:#1b1e27;--txt:#e8eaf0;--mut:#8b91a0;
     --acc:#e60023;--line:#262a35}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);
     font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
header{position:sticky;top:0;z-index:10;background:rgba(14,15,19,.92);
       backdrop-filter:blur(10px);border-bottom:1px solid var(--line);
       padding:14px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.brand{font-weight:800;font-size:20px;letter-spacing:-.5px;color:var(--acc)}
form{display:flex;flex:1 1 320px;gap:8px}
input{flex:1;padding:11px 14px;font-size:15px;border-radius:24px;
      border:1px solid var(--line);background:var(--card);color:var(--txt);
      outline:none;transition:border .15s}
input:focus{border-color:#4a5266}
button{padding:11px 18px;font-size:15px;font-weight:600;border:0;
      border-radius:24px;background:var(--acc);color:#fff;cursor:pointer}
button:active{transform:scale(.97)}
#status{color:var(--mut);font-size:13px;width:100%;padding-top:2px;
        min-height:18px}
#grid{columns:5 220px;column-gap:12px;padding:16px;max-width:1600px;
     margin:0 auto}
.card{break-inside:avoid;margin-bottom:12px;border-radius:12px;overflow:hidden;
      position:relative;background:var(--card);cursor:zoom-in;
      transition:transform .15s}
.card:hover{transform:translateY(-2px)}
.card img{width:100%;display:block}
.card .cap{position:absolute;left:0;right:0;bottom:0;padding:22px 10px 8px;
     background:linear-gradient(transparent,rgba(0,0,0,.75));
     font-size:12px;color:#fff;opacity:0;transition:opacity .15s}
.card:hover .cap{opacity:1}
#sentinel{height:48px;display:flex;align-items:center;justify-content:center;
          color:var(--mut);font-size:14px}
.spin{width:18px;height:18px;border:2px solid var(--line);
      border-top-color:var(--acc);border-radius:50%;margin-right:8px;
      display:inline-block;animation:r 1s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
#lb{position:fixed;inset:0;background:rgba(5,6,9,.94);z-index:50;display:none;
   flex-direction:column;align-items:center;justify-content:center;padding:16px}
#lb img{max-width:94vw;max-height:80vh;border-radius:8px;
        background:var(--card)}
#lb .bar{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;
         justify-content:center;align-items:center;color:var(--mut);font-size:13px}
#lb .bar a{color:#9fc1ff;text-decoration:none;font-weight:600}
#lb .close{position:absolute;top:14px;right:18px;font-size:26px;color:#fff;
           cursor:pointer;background:none;border:0}
.empty{padding:70px 20px;text-align:center;color:var(--mut)}
.empty .big{font-size:44px;margin-bottom:10px}
footer{text-align:center;color:#555;font-size:12px;padding:10px 0 24px}
</style></head><body>
<header>
 <span class="brand">pins</span>
 <form id="f"><input id="q" type="text" placeholder="search pinterest images..."
        autocomplete="off"><button>Search</button></form>
 <div id="status"></div>
</header>
<div class="empty" id="hero"><div class="big">🔍</div>
 Search Pinterest images &mdash; original quality, no logos or banners.</div>
<div id="grid"></div>
<div id="sentinel"><span class="spin" id="spin" style="display:none"></span><span id="smsg"></span></div>
<div id="lb">
 <button class="close" onclick="lb.style.display='none'">✕</button>
 <img id="lbimg" alt="">
 <div class="bar"><span id="lbinfo"></span>
  <a id="lbopen" target="_blank" rel="noopener">open original ↗</a>
  <a id="lbpin" target="_blank" rel="noopener">pin ↗</a></div>
</div>
<footer>developer @techno_telex</footer>
<script>
const grid = document.getElementById('grid'), st = document.getElementById('status'),
      sent = document.getElementById('sentinel'), lb = document.getElementById('lb'),
      hero = document.getElementById('hero'),
      spin = document.getElementById('spin'), smsg = document.getElementById('smsg');
let q = new URLSearchParams(location.search).get('q') || '',
    busy = false, more = true, total = 0;
document.getElementById('q').value = q;
document.getElementById('f').onsubmit = e => {
  e.preventDefault();
  location.search = '?q=' + encodeURIComponent(document.getElementById('q').value);
};
function card(r) {
  const d = document.createElement('div');
  d.className = 'card';
  d.innerHTML = '<img loading="lazy" src="' + r.url + '" alt="">' +
    '<div class="cap">' + (r.width||'?') + ' &times; ' + (r.height||'?') + '</div>';
  d.onclick = () => {
    document.getElementById('lbimg').src = r.orig_url || r.url;
    document.getElementById('lbinfo').textContent =
      (r.width||'?') + ' x ' + (r.height||'?') + ' px';
    document.getElementById('lbopen').href = r.orig_url || r.url;
    document.getElementById('lbpin').href = r.pin || '#';
    lb.style.display = 'flex';
  };
  grid.appendChild(d);
}
document.onkeydown = e => { if (e.key === 'Escape') lb.style.display = 'none'; };
lb.onclick = e => { if (e.target === lb) lb.style.display = 'none'; };
async function load() {
  if (!q || busy || !more) return;
  busy = true;
  spin.style.display = 'inline-block'; smsg.textContent = 'loading more…';
  try {
    const r = await fetch('/api/page?q=' + encodeURIComponent(q) + '&size=474x');
    const d = await r.json();
    if (d.error) { st.textContent = d.error; more = false; return; }
    hero.style.display = 'none';
    d.results.forEach(card);
    total += d.results.length;
    more = d.has_more;
    st.textContent = total + (total === 1 ? ' image' : ' images') +
      (more ? '' : ' · end of results');
  } catch (e) { st.textContent = 'network error: ' + e; }
  busy = false;
  spin.style.display = 'none'; smsg.textContent = '';
  if (more && inView()) load();   // fill the screen if it still isn't full
}
function inView() {
  const r = sent.getBoundingClientRect();
  return r.top < (window.innerHeight || document.documentElement.clientHeight) + 200;
}
new IntersectionObserver(es => { if (es[0].isIntersecting) load(); })
  .observe(sent);
if (q) load(); else st.textContent = '';
</script></body></html>"""


@app.route("/")
def index():
    return app.response_class(PAGE, mimetype="text/html")


if __name__ == "__main__":
    # run clean: no Flask banner, no dev-server warning, no request logs
    from flask import cli as flask_cli
    logging.getLogger("werkzeug").disabled = True
    flask_cli.show_server_banner = lambda *a, **k: None
    print(f"pins running on http://{HOST}:{PORT}", flush=True)
    app.run(host=HOST, port=PORT)

#!/usr/bin/env python3
"""
breathe.py — resilient Flask auto-pinger + forwarder

Features:
 - GET  /             -> status JSON (shows forward_to list)
 - GET  /send_wave    -> send a single GET to TARGET_URL
 - POST /receive_pulse-> forward the incoming JSON/form to FORWARD_URLS (with retries)
 - Background auto-pinger posts pulses at random intervals (MIN_INTERVAL..MAX_INTERVAL)
 - Wake-first logic: do a quick GET to the target base (root or /ping) to wake sleeping apps
 - Configurable timeouts, retries, backoff, and per-target delay via environment variables

ENV vars (new ones added):
 - TIMEOUT           : seconds for requests timeout (default 30)
 - RETRIES           : number of attempts for POST (default 3)
 - BACKOFF_BASE      : base seconds for exponential backoff (default 2.0)
 - WAKE_FIRST        : "true"/"1"/"yes" to try a quick GET to base before POST (default true)
 - PING_PATH         : quick wake path to try first (default "/ping" then "/")
 - LOG_PREFIX        : prefix shown in logs (default "breathe")
 - ... (see original docstring above for other envs)
"""

import os
import time
import random
import threading
import json
import sys
from urllib.parse import urlparse
from flask import Flask, request, jsonify

# optional requests
try:
    import requests
except Exception:
    requests = None

# ------------------------
# Defaults and env config
# ------------------------
DEFAULT_TARGET_BASE = os.environ.get("TARGET_URL", "https://exercise-go9d.onrender.com").strip()
DEFAULT_PULSE_PATH = "/pulse_receiver"

DEFAULT_FORWARD_URLS = [
    "https://exercise-go9d.onrender.com",
    "https://who-i-am-uzh6.onrender.com",
    "https://tomorrow-personal-app.onrender.com",
    "https://breathe-5006.onrender.com",
    "https://church-i0im.onrender.com",
    # jevicarn intentionally omitted per request
]

# Read env
TARGET_URL = os.environ.get("TARGET_URL", DEFAULT_TARGET_BASE).strip()
LEGACY_FORWARD = os.environ.get("FORWARD_URL", "").strip()
raw_list = os.environ.get("FORWARD_URLS", "").strip()
FORWARD_TOKEN = os.environ.get("FORWARD_TOKEN")  # optional X-PULSE-TOKEN
AUTO_PING = os.environ.get("AUTO_PING", "true").lower() in ("1", "true", "yes")

# intervals
try:
    MIN_INTERVAL = float(os.environ.get("MIN_INTERVAL", "15"))
    MAX_INTERVAL = float(os.environ.get("MAX_INTERVAL", "49"))
except Exception:
    MIN_INTERVAL, MAX_INTERVAL = 15.0, 49.0

try:
    PER_TARGET_DELAY = float(os.environ.get("PER_TARGET_DELAY", "0.15"))
except Exception:
    PER_TARGET_DELAY = 0.15

# New robustness options
try:
    TIMEOUT = float(os.environ.get("TIMEOUT", "30"))          # seconds for requests
except Exception:
    TIMEOUT = 30.0

try:
    RETRIES = int(os.environ.get("RETRIES", "3"))             # attempts per POST
except Exception:
    RETRIES = 3

try:
    BACKOFF_BASE = float(os.environ.get("BACKOFF_BASE", "2.0"))
except Exception:
    BACKOFF_BASE = 2.0

WAKE_FIRST = os.environ.get("WAKE_FIRST", "true").lower() in ("1", "true", "yes")
PING_PATHS = [p.strip() for p in os.environ.get("PING_PATH", "/ping,/").split(",") if p.strip()]

LOG_PREFIX = os.environ.get("LOG_PREFIX", "breathe")

# sanitize
if MIN_INTERVAL <= 0 or MAX_INTERVAL <= 0 or MIN_INTERVAL > MAX_INTERVAL:
    MIN_INTERVAL, MAX_INTERVAL = 15.0, 49.0
if PER_TARGET_DELAY < 0:
    PER_TARGET_DELAY = 0.0
if TIMEOUT < 1:
    TIMEOUT = 30.0
if RETRIES < 1:
    RETRIES = 1

# ------------------------
# Helpers
# ------------------------
def _log(*a, **k):
    pre = f"[{LOG_PREFIX}]"
    print(pre, *a, **k)
    sys.stdout.flush()

def normalize_target_candidate(candidate: str) -> str:
    """Return URL that ends with /pulse_receiver (no duplicated segments)."""
    if not candidate:
        return None
    c = candidate.strip()
    if not c:
        return None
    c = c.rstrip()
    lower = c.lower().rstrip('/')
    while lower.endswith('/pulse_receiver'):
        c = c[: -len('/pulse_receiver')].rstrip('/')
        lower = c.lower().rstrip('/')
    # append single path
    return c.rstrip('/') + DEFAULT_PULSE_PATH

def base_from_pulse_url(pulse_url: str) -> str:
    """From .../pulse_receiver -> return base like https://example.com"""
    if not pulse_url:
        return None
    if pulse_url.endswith(DEFAULT_PULSE_PATH):
        return pulse_url[:-len(DEFAULT_PULSE_PATH)]
    # defensive: strip path component
    parsed = urlparse(pulse_url)
    return f"{parsed.scheme}://{parsed.netloc}"

# safe check for HTTP response success
def is_success_code(code):
    return isinstance(code, int) and 200 <= code < 300

# perform a GET to a wake URL (try multiple PING_PATHS), returns True if any returned 2xx/3xx/200-399
def try_wake_target(session_obj, base):
    if not session_obj:
        return False
    for p in PING_PATHS:
        try:
            url = base.rstrip('/') + (p if p.startswith('/') else '/' + p)
            _log("wake: GET", url)
            r = session_obj.get(url, timeout=min(10, TIMEOUT))
            if r.status_code >= 200 and r.status_code < 400:
                _log("wake: success", url, "->", r.status_code)
                return True
            _log("wake: non-success", url, "->", r.status_code)
        except Exception as e:
            _log("wake: error", base, "->", e)
    # fallback: try root
    try:
        r = session_obj.get(base.rstrip('/') + '/', timeout=min(10, TIMEOUT))
        if r.status_code >= 200 and r.status_code < 400:
            _log("wake: root success", base, "->", r.status_code)
            return True
    except Exception as e:
        _log("wake: root error", base, "->", e)
    return False

def post_with_retries(session_obj, url, payload, headers=None, retries=RETRIES):
    """POST json payload to url with retries and exponential backoff. Returns dict with result."""
    if not session_obj:
        return {"url": url, "error": "requests not available"}
    headers = headers or {}
    attempt = 0
    last_err = None
    while attempt < retries:
        attempt += 1
        try:
            r = session_obj.post(url, json=payload, headers=headers, timeout=TIMEOUT)
            txt = (r.text[:400] if r.text else "")
            if is_success_code(r.status_code):
                return {"url": url, "code": r.status_code, "text_snippet": txt, "attempts": attempt}
            # non-2xx indicates server responded but with error; capture and possibly retry
            last_err = f"status={r.status_code} text={txt}"
            _log("post_with_retries: non-success", url, "->", r.status_code)
        except Exception as e:
            last_err = str(e)
            _log("post_with_retries: exception", url, "->", e)
        # backoff before next attempt
        if attempt < retries:
            sleep_for = BACKOFF_BASE ** (attempt - 1)
            # clamp a sensible cap
            if sleep_for > 30:
                sleep_for = 30
            _log(f"post_with_retries: sleeping {sleep_for:.1f}s before retry to {url}")
            time.sleep(sleep_for)
    return {"url": url, "error": last_err, "attempts": attempt}

# ------------------------
# Build final FORWARD_URLS list
# ------------------------
FORWARD_URLS = []
if raw_list:
    items = [i.strip() for i in raw_list.split(',') if i.strip()]
    FORWARD_URLS.extend(items)
elif LEGACY_FORWARD:
    FORWARD_URLS.append(LEGACY_FORWARD)
else:
    FORWARD_URLS.extend(DEFAULT_FORWARD_URLS)

# normalize
FORWARD_URLS = [normalize_target_candidate(u) for u in FORWARD_URLS if u]
# remove duplicates preserving order
seen = set()
final_targets = []
for u in FORWARD_URLS:
    if u not in seen:
        seen.add(u)
        final_targets.append(u)
FORWARD_URLS = final_targets

# ------------------------
# Flask app + session
# ------------------------
app = Flask(__name__)
_start_time = time.time()
session = requests.Session() if requests else None

_log("breathe: forward targets:")
for t in FORWARD_URLS:
    _log(" -", t)
_log("breathe: TIMEOUT", TIMEOUT, "RETRIES", RETRIES, "WAKE_FIRST", WAKE_FIRST, "PING_PATHS", PING_PATHS)

# ------------------------
# Routes
# ------------------------
@app.route("/")
def root():
    return jsonify({
        "status": "alive",
        "uptime_seconds": int(time.time() - _start_time),
        "auto_ping": AUTO_PING,
        "min_interval": MIN_INTERVAL,
        "max_interval": MAX_INTERVAL,
        "forward_to": FORWARD_URLS,
        "per_target_delay": PER_TARGET_DELAY,
        "timeout": TIMEOUT,
        "retries": RETRIES
    })

@app.route("/status")
def status():
    return root()

@app.route("/send_wave", methods=["GET"])
def send_wave():
    """Send a single GET to TARGET_URL (useful for testing)."""
    if not session:
        return jsonify({"status": "error", "error": "requests not installed"}), 500
    try:
        r = session.get(TARGET_URL, timeout=TIMEOUT)
        return jsonify({"status": "ok", "target": TARGET_URL, "code": r.status_code}), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/receive_pulse", methods=["POST", "GET"])
def receive_pulse():
    """
    Accept inbound pulse and forward it to all FORWARD_URLS as POST (JSON).
    Returns per-target summary.
    """
    if not session:
        return jsonify({"status": "error", "error": "requests not installed"}), 500

    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict() or {"message": "ping"}

    headers = {}
    if FORWARD_TOKEN:
        headers["X-PULSE-TOKEN"] = FORWARD_TOKEN

    results = []
    for idx, u in enumerate(FORWARD_URLS):
        base = base_from_pulse_url(u)
        # optionally try to wake target quickly
        if WAKE_FIRST and base:
            try_wake_target(session, base)
            # tiny pause before actual POST
            time.sleep(0.05)
        res = post_with_retries(session, u, payload, headers=headers, retries=RETRIES)
        results.append(res)
        # small stagger
        if PER_TARGET_DELAY and idx != len(FORWARD_URLS) - 1:
            time.sleep(PER_TARGET_DELAY)

    return jsonify({"status": "forwarded_to_multiple", "results": results}), 200

# ------------------------
# Background auto-pinger
# ------------------------
def auto_ping_loop():
    """Daemon loop — posts to each FORWARD_URL at random intervals between MIN_INTERVAL and MAX_INTERVAL."""
    if not session:
        _log("auto_ping: requests not available; auto pinger disabled")
        return
    _log(f"auto_ping: starting loop -> forwarding to {len(FORWARD_URLS)} targets every {MIN_INTERVAL}-{MAX_INTERVAL}s (random)")
    while True:
        wait = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
        _log(f"auto_ping: sleeping {wait:.2f}s")
        time.sleep(wait)
        payload = {"source": "breathe", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        headers = {}
        if FORWARD_TOKEN:
            headers["X-PULSE-TOKEN"] = FORWARD_TOKEN
        for idx, u in enumerate(FORWARD_URLS):
            base = base_from_pulse_url(u)
            if WAKE_FIRST and base:
                try_wake_target(session, base)
                # tiny pause so root sees slightly different timestamps
                time.sleep(0.05)
            res = post_with_retries(session, u, payload, headers=headers, retries=RETRIES)
            if "code" in res:
                _log(f"auto_ping: OK {u} -> {res['code']} (attempts {res.get('attempts')})")
            else:
                _log(f"auto_ping: FAIL {u} -> {res.get('error')} (attempts {res.get('attempts')})")
            if PER_TARGET_DELAY and idx != len(FORWARD_URLS) - 1:
                time.sleep(PER_TARGET_DELAY)

# start pinger if enabled
if AUTO_PING:
    t = threading.Thread(target=auto_ping_loop, name="auto_ping", daemon=True)
    t.start()
else:
    _log("auto_ping: disabled (set AUTO_PING=true to enable)")

# ------------------------
# CLI helper: send once to targets then exit
# ------------------------
def send_once_and_exit():
    if not requests:
        print("requests not installed", file=sys.stderr)
        raise SystemExit(1)
    payload = {"source": "breathe-cli", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    headers = {}
    if FORWARD_TOKEN:
        headers["X-PULSE-TOKEN"] = FORWARD_TOKEN
    ok = []
    for u in FORWARD_URLS:
        base = base_from_pulse_url(u)
        if WAKE_FIRST and base:
            try_wake_target(session, base)
            time.sleep(0.05)
        res = post_with_retries(session, u, payload, headers=headers, retries=RETRIES)
        if "code" in res and is_success_code(res["code"]):
            print(f"POST {u} -> {res['code']} (attempts {res.get('attempts')})")
            ok.append((u, res["code"]))
        else:
            print(f"ERROR posting to {u}: {res.get('error')} (attempts {res.get('attempts')})", file=sys.stderr)
            ok.append((u, res.get('error')))
    raise SystemExit(0 if any(isinstance(s, int) and s < 400 for _, s in ok) else 2)

# ------------------------
# Run
# ------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Send one pulse to all FORWARD_URLS then exit")
    args = parser.parse_args()
    if args.once:
        send_once_and_exit()

    port = int(os.environ.get("PORT", 5001))
    # dev server for local testing; in prod use gunicorn
    app.run(host="0.0.0.0", port=port)

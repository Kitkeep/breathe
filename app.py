#!/usr/bin/env python3
"""
breathe.py — small Flask app + background auto-pinger (multi-target, fixed path handling).

Behavior:
 - GET  /             -> status JSON (shows forward_to list)
 - GET  /send_wave    -> send a single GET to TARGET_URL
 - POST /receive_pulse-> forward the incoming JSON/form to FORWARD_URLS (with optional X-PULSE-TOKEN)
 - Background thread (daemon) optionally sends POST pulses to FORWARD_URLS at random intervals
   between MIN_INTERVAL and MAX_INTERVAL.

ENV vars:
 - TARGET_URL       : where /send_wave will GET (default: https://exercise-go9d.onrender.com)
 - FORWARD_URL      : legacy single forward target (kept for compatibility)
 - FORWARD_URLS     : comma-separated list of forward targets (preferred). Can be full path or base. The code
                      normalizes so each entry ends with /pulse_receiver (but will NOT duplicate it).
 - FORWARD_TOKEN    : optional header value X-PULSE-TOKEN when forwarding
 - AUTO_PING        : "true"/"1"/"yes" to enable background pinger (default: true)
 - MIN_INTERVAL     : min seconds for random interval (default: 15)
 - MAX_INTERVAL     : max seconds for random interval (default: 49)
 - PER_TARGET_DELAY : seconds to sleep between posting to targets (default: 0.15)
 - PORT             : port to listen on (default 5001)
"""
import os
import time
import random
import threading
import json
import sys
from flask import Flask, request, jsonify

# defensive requests import — app runs without requests but network ops fail gracefully
try:
    import requests
except Exception:
    requests = None

# ------------------------
# Default targets
# ------------------------
# set your site as default target so send_wave and pinging will hit it by default
DEFAULT_TARGET_BASE = "https://exercise-go9d.onrender.com"
DEFAULT_PULSE_PATH = "/pulse_receiver"

# default list of all important targets (bases; normalization will append /pulse_receiver)
DEFAULT_FORWARD_URLS = [
    "https://exercise-go9d.onrender.com",
    "https://tomorrow-personal-app.onrender.com",
    "https://breathe-5006.onrender.com",
    "https://church-i0im.onrender.com",
    "https://beacon-cloud.onrender.com",
    "https://tomorrowapp.onrender.com",
    "https://e-agriculture.onrender.com",
    "https://davidsconnect.onrender.com"
]

# ------------------------
# Config (env-driven)
# ------------------------
TARGET_URL = os.environ.get("TARGET_URL", DEFAULT_TARGET_BASE).strip()
LEGACY_FORWARD = os.environ.get("FORWARD_URL", "").strip()
raw_list = os.environ.get("FORWARD_URLS", "").strip()
# incoming/outgoing token header name is X-PULSE-TOKEN; set FORWARD_TOKEN to include it when sending
FORWARD_TOKEN = os.environ.get("FORWARD_TOKEN")  # optional X-PULSE-TOKEN
AUTO_PING = os.environ.get("AUTO_PING", "true").lower() in ("1", "true", "yes")

# intervals
try:
    MIN_INTERVAL = float(os.environ.get("MIN_INTERVAL", "15"))
    MAX_INTERVAL = float(os.environ.get("MAX_INTERVAL", "49"))
except Exception:
    MIN_INTERVAL = 15.0
    MAX_INTERVAL = 49.0

try:
    PER_TARGET_DELAY = float(os.environ.get("PER_TARGET_DELAY", "0.15"))
except Exception:
    PER_TARGET_DELAY = 0.15

# sanitize sane values
if MIN_INTERVAL <= 0 or MAX_INTERVAL <= 0 or MIN_INTERVAL > MAX_INTERVAL:
    MIN_INTERVAL = 15.0
    MAX_INTERVAL = 49.0
if PER_TARGET_DELAY < 0:
    PER_TARGET_DELAY = 0.0

# ------------------------
# Helper to normalize target URLs (robust: removes any existing /pulse_receiver fragments then appends one)
# ------------------------
def normalize_target_candidate(candidate: str) -> str:
    """
    Accept candidate that may be:
      - a full URL ending with /pulse_receiver or not
      - a base like https://example.com (append /pulse_receiver)
    Return definitive URL that ends with /pulse_receiver (no duplicated segments).
    """
    if not candidate:
        return None
    c = candidate.strip()
    if not c:
        return None
    # strip trailing whitespace/slashes
    c = c.rstrip()
    # remove any number of trailing '/pulse_receiver' (case-insensitive)
    # so we avoid duplication like '/pulse_receiver/pulse_receiver'
    lower = c.lower().rstrip('/')
    while lower.endswith('/pulse_receiver'):
        # remove last segment equal to '/pulse_receiver'
        c = c[: -len('/pulse_receiver')].rstrip('/')
        lower = c.lower().rstrip('/')
    # now re-append single pulse path
    return c.rstrip('/') + DEFAULT_PULSE_PATH

# ------------------------
# Build final FORWARD_URLS list
# ------------------------
FORWARD_URLS = []

# 1. Environment variable (preferred)
if raw_list:
    items = [i.strip() for i in raw_list.split(',') if i.strip()]
    FORWARD_URLS.extend(items)

# 2. Legacy single forward
elif LEGACY_FORWARD:
    FORWARD_URLS.append(LEGACY_FORWARD)

# 3. Default all important targets (only if no env overrides)
else:
    FORWARD_URLS.extend(DEFAULT_FORWARD_URLS)

# normalize
FORWARD_URLS = [normalize_target_candidate(u) for u in FORWARD_URLS if u]

# remove duplicates, preserve order
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

def _log(*a, **k):
    print(*a, **k)
    sys.stdout.flush()

# Log final forward targets at startup
_log("breathe: forward targets:")
for t in FORWARD_URLS:
    _log("  -", t)

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
        "per_target_delay": PER_TARGET_DELAY
    })

@app.route("/send_wave", methods=["GET"])
def send_wave():
    """Send a single GET to TARGET_URL (useful for testing)."""
    if not session:
        return jsonify({"status": "error", "error": "requests not installed"}), 500
    try:
        r = session.get(TARGET_URL, timeout=10)
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
        try:
            r = session.post(u, json=payload, headers=headers, timeout=10)
            txt = (r.text[:300] if r.text else "")
            results.append({"url": u, "code": r.status_code, "text_snippet": txt})
            _log(f"receive_pulse: forwarded to {u} -> {r.status_code}")
        except Exception as e:
            results.append({"url": u, "error": str(e)})
            _log(f"receive_pulse: error forwarding to {u}: {e}")
        # small stagger to avoid hammering
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
            try:
                r = session.post(u, json=payload, headers=headers, timeout=10)
                _log(f"auto_ping: POST {u} -> {r.status_code}")
            except Exception as e:
                _log(f"auto_ping: error posting to {u}: {e}")
            # tiny stagger so multiple targets don't get the exact same timestamp
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
        try:
            r = session.post(u, json=payload, headers=headers, timeout=10)
            print(f"POST {u} -> {r.status_code}")
            ok.append((u, r.status_code))
        except Exception as e:
            print(f"ERROR posting to {u}: {e}", file=sys.stderr)
            ok.append((u, str(e)))
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


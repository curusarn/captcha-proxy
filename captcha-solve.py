#!/usr/bin/env python3
"""captcha-solve --url <page URL> <screenshot.png>

One round of a CAPTCHA (reCAPTCHA, hCaptcha, a challenge checkbox) solved by a
vision GLM model through OpenRouter: prints where to click_at (viewport pixels)
and what to do next. The browser wrapper's proxy (captcha-proxy.py) runs this
loop on its own; the CLI is for a manual round. Needs OPENROUTER_API_KEY.

Exit 0: an answer to act on. 2: the site is blocklisted. 3: GLM can't read it.
CAPTCHA_BLOCKLIST overrides the built-in BLOCKLIST (see blocklist()).
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# ── Blocklist: sites the solver never touches ──────────────────────────────
# Seeded 2026-11-04 from the risks of solving CAPTCHAs automatically; Simon
# edits it. A CAPTCHA on these stays unsolved: the agent routes around it (an
# API, a session we already have) or does without. An entry matches its host
# and every subdomain. CAPTCHA_BLOCKLIST replaces this list (see blocklist()).
BLOCKLIST = [
    # Sign-ins of our real accounts: Google, Microsoft and Slack flag or lock
    # accounts that sign in through automation.
    "accounts.google.com",
    "login.microsoftonline.com", "login.live.com", "account.microsoft.com",
    "slack.com",
    "appleid.apple.com", "idmsa.apple.com",
    "github.com",
    "1password.com",
    # Money and identity.
    "stripe.com", "paypal.com", "wise.com", "revolut.com",
    # Government and tax portals.
    "gov.cz", "mojedatovaschranka.cz", "gov.uk", "irs.gov",
    # Competitors: their sign-ups go through a made-up identity (the
    # greg.gorski inbox), so no automated way past their bot checks.
    "firehydrant.com", "rootly.com", "atlassian.com", "atlassian.net",
    "opsgenie.com", "statuspage.io", "sentry.io", "dash0.com", "incident.io",
    "pagerduty.com", "datadoghq.com", "datadoghq.eu", "grafana.com",
    "grafana.net", "newrelic.com", "honeycomb.io", "honeybadger.io",
    "chronosphere.io", "checklyhq.com", "uptimerobot.com", "pingdom.com",
    "instatus.com", "squadcast.com", "splunk.com", "elastic.co", "logz.io",
    "mezmo.com", "logdna.com", "axiom.co", "highlight.io", "posthog.com",
    "signoz.io", "uptrace.dev", "coralogix.com", "sumologic.com",
    "dynatrace.com", "groundcover.com", "kloudfuse.com", "last9.io",
    "hyperdx.io", "openobserve.ai", "ilert.com", "zenduty.com", "spike.sh",
    "solarwinds.com", "papertrail.com", "papertrailapp.com", "loggly.com",
    "site24x7.com", "statuscake.com", "cronitor.io", "hyperping.io",
    "hyperping.com", "openstatus.dev", "onlineornot.com", "sematext.com",
    "lightstep.com", "xmatters.com", "appsignal.com", "bugsnag.com",
    "rollbar.com", "raygun.com", "logrocket.com", "middleware.io",
    "uptime.com",
]

API_URL = "https://openrouter.ai/api/v1/chat/completions"
# Fast and placed tiles exactly on betterstack.com's grid (2026-11-04);
# z-ai/glm-5v-turbo took 30 s and its coordinates were off.
MODEL = os.environ.get("CAPTCHA_MODEL", "z-ai/glm-5.3-flashx")

PROMPT = """This is a screenshot (top-left origin, pixel coordinates) of a web page
with a CAPTCHA (reCAPTCHA, hCaptcha or a "verify you are human" checkbox).
Describe what you see; another program decides the clicks.

If it shows an image grid ("Select all images with X", "Select all squares
with X", "Please click each image containing X"), judge EVERY tile, one by one
in reading order:
- "state": "selected" (tick or highlight), "blank" (white/fading, refilling)
  or "image".
- "match": true when that tile's picture shows X, even small, partly cut off,
  or behind a heavy filter/noise; false otherwise. Look hard at each tile on
  its own; a filtered tile without X is simply false.
- "center": the tile's center [x, y] in screenshot pixels.

Respond with ONLY a JSON object, no other text:
{"kind": "grid" | "checkbox" | "none" | "unreadable",
 "target": "X, as the challenge names it",
 "dynamic": true if it says "Click verify once there are none left",
 "tiles": [{"row": 1, "col": 1, "state": "image", "match": false, "center": [x, y]}, ...],
 "checkbox": [x, y] for an unticked "I'm not a robot" / "Verify you are human" box, else null,
 "verify_point": [x, y] of the VERIFY / SKIP / NEXT button, else null,
 "reload_point": [x, y] of the reload arrow ("Get a new challenge"), else null,
 "say": "one short line on what you see"}
"kind": "none" when no CAPTCHA is visible or the checkbox is already ticked
with no grid; "unreadable" only when you can't make out the challenge."""


class SolveError(Exception):
    pass


def blocklist():
    """The domains in force: CAPTCHA_BLOCKLIST when set, else BLOCKLIST.
    CAPTCHA_BLOCKLIST is comma- or space-separated; the word "default" in it
    adds the built-in BLOCKLIST, and an empty value blocks nothing."""
    env = os.environ.get("CAPTCHA_BLOCKLIST")
    if env is None:
        return list(BLOCKLIST)
    out = []
    for d in env.replace(",", " ").split():
        d = d.strip().lower().lstrip(".")
        out += BLOCKLIST if d == "default" else [d]
    return out


def blocked(url):
    """The blocklist entry that covers url's host, or None."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for d in blocklist():
        if host == d or host.endswith("." + d):
            return d
    return None


def key():
    """The OpenRouter API key, from the OPENROUTER_API_KEY environment variable."""
    k = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not k:
        raise SolveError("OPENROUTER_API_KEY is not set")
    return k


def ask(png_bytes):
    body = json.dumps({
        "model": MODEL,
        "temperature": 0,
        # Default reasoning spent ~4000 tokens (34 s) per round, past the
        # challenge's 2-minute expiry; minimal answers in ~4 s, as accurate.
        "reasoning": {"effort": os.environ.get("CAPTCHA_EFFORT", "minimal")},
        # Zero data retention: a page screenshot can show anything, so OpenRouter
        # may route it only to endpoints that keep no prompts.
        "provider": {"zdr": True},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()}}]}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": "Bearer " + key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        raise SolveError(f"OpenRouter {e.code}: {e.read().decode()[:300]}")
    except Exception as e:
        raise SolveError(f"OpenRouter: {e}")
    text = (out["choices"][0]["message"].get("content") or "").strip()
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        raise SolveError(f"GLM's answer isn't JSON: {text[:200]}")


def decide(png_bytes):
    """One round: {"points", "then", "verify_point", "reload_point", "say"}.
    then: "verify" (click the points, then VERIFY), "screenshot" (click the
    points, wait for refills, look again), "done" (no CAPTCHA visible) or
    "cannot" (GLM can't read it: get a new challenge)."""
    ans = ask(png_bytes)
    kind, tiles = ans.get("kind"), ans.get("tiles") or []
    pts, then = [], "cannot"
    if kind == "checkbox" and ans.get("checkbox"):
        pts, then = [ans["checkbox"]], "screenshot"
    elif kind == "grid":
        pts = [t["center"] for t in tiles if t.get("match")
               and t.get("state") == "image" and t.get("center")]
        refilling = any(t.get("state") == "blank" for t in tiles)
        if ans.get("dynamic") and len(tiles) == 9:  # only 3x3 grids refill
            # Picked tiles refill; verify only once a full grid shows no match.
            then = "screenshot" if pts or refilling else "verify"
        else:
            then = "verify"   # fixed grids never refill: pick, then VERIFY
    elif kind == "none":
        then = "done"
    rnd = lambda p: [round(p[0]), round(p[1])] if p else None
    return {"points": [rnd(p) for p in pts], "then": then,
            "verify_point": rnd(ans.get("verify_point")),
            "reload_point": rnd(ans.get("reload_point")),
            "target": ans.get("target"), "say": ans.get("say", "")}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True, help="the page's URL (blocklist check)")
    ap.add_argument("screenshot", help="viewport screenshot (PNG) of that page")
    a = ap.parse_args()
    hit = blocked(a.url)
    if hit:
        print(f"captcha-solve: {a.url} is on the blocklist ({hit}): leave this "
              "CAPTCHA alone; route around it or do without.", file=sys.stderr)
        sys.exit(2)
    try:
        with open(a.screenshot, "rb") as f:
            d = decide(f.read())
    except SolveError as e:
        sys.exit(f"captcha-solve: {e}")
    print(json.dumps(d))
    print(f"GLM: {d['say']}", file=sys.stderr)
    for x, y in d["points"]:
        print(f"  click_at x={x} y={y}", file=sys.stderr)
    if d["then"] == "verify":
        vp = d["verify_point"]
        print(f"  click_at x={vp[0]} y={vp[1]} (VERIFY)" if vp else "  click VERIFY",
              file=sys.stderr)
    elif d["then"] == "screenshot":
        print("  wait 4 s, take a new screenshot, run again", file=sys.stderr)
    elif d["then"] == "cannot":
        rp = d["reload_point"]
        print(f"  can't read it: click_at x={rp[0]} y={rp[1]} for a new challenge"
              if rp else "  can't read it: get a new challenge", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()

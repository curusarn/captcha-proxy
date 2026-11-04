#!/usr/bin/env python3
"""selftest.py <browser MCP command...>

Checks a proxied browser end to end on Google's reCAPTCHA demo page
(https://www.google.com/recaptcha/api2/demo): opens it, lets the proxy pass the
CAPTCHA, submits the form and looks for "Verification Success".

  python3 selftest.py ~/.local/bin/chrome-devtools-mcp-agent
  python3 selftest.py python3 captcha-proxy.py chrome-devtools-mcp --headless --experimentalVision

Exit 0 when Google confirmed the solve. The proxy's own log is
~/.cache/captcha-proxy.log.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

DEMO = "https://www.google.com/recaptcha/api2/demo"
LOG = os.path.expanduser("~/.cache/captcha-proxy.log")


def follow_log(stop):
    """Prints the proxy's progress from its log as it happens, so a run that
    takes a minute never looks stuck."""
    try:
        f = open(LOG, "a+")
        f.seek(0, os.SEEK_END)
    except OSError:
        return
    while not stop.is_set():
        line = f.readline()
        if not line:
            time.sleep(0.3)
            continue
        if " round " in line:
            n, data = re.search(r" round (\d+) (\{.*\})", line).groups()
            d = json.loads(data)
            print(f"  round {n}: {d.get('target') or 'checkbox'}, {len(d.get('points') or [])} "
                  f"click(s), then {d.get('then')}", flush=True)
        elif " solving " in line:
            print("  CAPTCHA found, solving...", flush=True)
        elif " solved " in line:
            print("  solved", flush=True)
        elif " gave up " in line:
            print("  gave up", flush=True)
        elif " rejected" in line:
            print("  Google rejected the answer, re-opening the challenge", flush=True)
        elif " blocklisted " in line:
            print("  blocklisted: left alone", flush=True)
        elif " solver error " in line:
            print("  solver error: " + line.split(" solver error ", 1)[1].strip()[:120], flush=True)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    p = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    n = 0

    def rpc(method, params=None, notify=False):
        nonlocal n
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            n += 1
            msg["id"] = n
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()
        while not notify:
            line = p.stdout.readline()
            if not line:
                sys.exit("selftest: the MCP server exited")
            r = json.loads(line)
            if r.get("id") == n:
                return r

    def tool(name, **args):
        t = time.time()
        res = rpc("tools/call", {"name": name, "arguments": args}).get("result") or {}
        txt = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        print(f"{name}: {time.time() - t:.1f}s", flush=True)
        return txt

    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "captcha-proxy-selftest", "version": "1"}})
    rpc("notifications/initialized", notify=True)
    tools = [t["name"] for t in rpc("tools/list", {})["result"]["tools"]]
    if "click_at" not in tools:
        sys.exit("selftest: no click_at tool: start chrome-devtools-mcp with --experimentalVision")
    stop = threading.Event()
    threading.Thread(target=follow_log, args=(stop,), daemon=True).start()
    print(f"opening {DEMO} (a solve takes 10-60 s)", flush=True)
    txt = tool("new_page", url=DEMO, isolatedContext="captcha-selftest")
    for line in txt.splitlines():
        if line.startswith("[captcha-proxy]"):
            print(line)
    page = int(re.search(r"^(\d+): .*\[selected\]", txt, re.M).group(1))
    ua = tool("evaluate_script", pageId=page, function="() => navigator.userAgent")
    print("user agent:", "headless" if "Headless" in ua else "plain")
    body = ""
    for attempt in range(2):
        # Submit; when the proxy only solved the CAPTCHA during that click, the
        # form went out without the token, so submit once more.
        snap = tool("take_snapshot", pageId=page)
        uid = re.search(r'uid=(\S+) button "Submit"', snap)
        if not uid:
            sys.exit("selftest: no Submit button on the demo page")
        clicked = tool("click", pageId=page, uid=uid.group(1))
        body = tool("evaluate_script", pageId=page, function="() => document.body.innerText")
        if "Verification Success" in body or "solved automatically" not in clicked:
            break
        print("solved during Submit: submitting again", flush=True)
    tool("close_page", pageId=page)
    stop.set()
    p.stdin.close()
    ok = "Verification Success" in body
    print("RESULT:", "PASS: Google says Verification Success" if ok else f"FAIL: {body[:200]!r}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

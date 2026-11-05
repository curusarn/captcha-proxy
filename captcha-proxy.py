#!/usr/bin/env python3
"""captcha-proxy <chrome-devtools-mcp command...>

A stdio MCP proxy in front of chrome-devtools-mcp that passes CAPTCHAs on its
own, so agents never have to. After each tool call that can bring one up (a
click, a key press, a navigation), it looks for a visible reCAPTCHA / hCaptcha
challenge or an unticked challenge checkbox. If one is there and the page is
not blocklisted (CAPTCHA_BLOCKLIST, else BLOCKLIST in captcha-solve.py), it solves it with GLM (screenshot ->
captcha-solve.decide -> click_at -> VERIFY, repeated) before returning the
tool's result, with one line added saying what happened.

Usage: put it in front of the MCP command, e.g.
  OPENROUTER_API_KEY=... captcha-proxy.py chrome-devtools-mcp --experimentalVision ...
chrome-devtools-mcp must run with --experimentalVision (click_at). The key is
not passed on to the MCP or Chrome. Log: ~/.cache/captcha-proxy.log.
"""
import asyncio
import base64
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location("captcha_solve",
                                              os.path.join(HERE, "captcha-solve.py"))
solver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solver)

LOG = os.path.expanduser("~/.cache/captcha-proxy.log")
# Tools after which a CAPTCHA can show up, and whether it can appear late
# (an invisible reCAPTCHA pops its challenge a moment after a submit click).
WATCH = {"click": True, "click_at": True, "press_key": True, "fill_form": True,
         "type_text": True, "navigate_page": False, "new_page": False}
# When Google distrusts the IP it chains new challenges forever; 20 rounds did no better than 10 (2026-11-05).
MAX_ROUNDS = int(os.environ.get("CAPTCHA_MAX_ROUNDS", "10"))

CHECK_JS = r"""async () => {
  const vis = f => { const r = f.getBoundingClientRect();
    return r.width > 40 && r.height > 40 &&
      f.checkVisibility({opacityProperty: true, visibilityProperty: true}); };
  const q = s => [...document.querySelectorAll(s)];
  const state = () => ({
    url: location.href,
    challenge: q('iframe[src*="/recaptcha/api2/bframe"], iframe[src*="/recaptcha/enterprise/bframe"], iframe[src*="hcaptcha.com"][src*="frame=challenge"]').some(vis),
    checkbox: q('iframe[src*="/recaptcha/api2/anchor"]:not([src*="size=invisible"]), iframe[src*="/recaptcha/enterprise/anchor"]:not([src*="size=invisible"]), iframe[src*="hcaptcha.com"][src*="frame=checkbox"], iframe[src*="challenges.cloudflare.com"]').some(vis),
    present: q('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"]').length > 0,
  });
  let s = state();
  for (let i = 0; __WAIT__ && s.present && !s.challenge && i < 6; i++) {
    await new Promise(r => setTimeout(r, 400)); s = state(); }
  return s;
}"""


# reCAPTCHA's own verdict: a token once Google accepted, "" while not, null
# where there is no reCAPTCHA API (hCaptcha, Turnstile).
TOKEN_JS = r"""() => { const g = window.grecaptcha && (window.grecaptcha.enterprise || window.grecaptcha);
  if (!g || !g.getResponse) return {token: null};
  try { return {token: g.getResponse() || ""}; } catch (e) { return {token: null}; } }"""
# Re-opens an invisible reCAPTCHA's challenge after Google rejected a round and hid it.
REOPEN_JS = r"""() => { const g = window.grecaptcha && (window.grecaptcha.enterprise || window.grecaptcha);
  try { g.execute(); return {ok: true}; } catch (e) { return {ok: false}; } }"""


def log(*a):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + " ".join(map(str, a)) + "\n")
    except Exception:
        pass


class Proxy:
    def __init__(self, cmd):
        self.cmd = cmd
        self.pending = {}      # our own request id -> future
        self.held = {}         # agent request id -> future (response we post-process)
        self.n = 0
        self.checkbox_done = set()   # (pageId, url) where we already ticked a checkbox
        self.out_lock = asyncio.Lock()

    async def write_out(self, msg):
        async with self.out_lock:
            sys.stdout.buffer.write((json.dumps(msg) + "\n").encode())
            sys.stdout.buffer.flush()

    async def write_child(self, msg):
        self.child.stdin.write((json.dumps(msg) + "\n").encode())
        await self.child.stdin.drain()

    async def call(self, name, args, timeout=60):
        self.n += 1
        rid = f"captcha-proxy-{self.n}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.write_child({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                "params": {"name": name, "arguments": args}})
        msg = await asyncio.wait_for(fut, timeout)
        return msg.get("result") or {}

    @staticmethod
    def text(result):
        return "\n".join(c.get("text", "") for c in result.get("content", [])
                         if c.get("type") == "text")

    async def state(self, page, wait):
        r = await self.call("evaluate_script",
                            {"pageId": page, "function": CHECK_JS.replace("__WAIT__", "true" if wait else "false")})
        m = re.search(r"```json\s*(\{.*?\})\s*```", self.text(r), re.S) or \
            re.search(r"(\{\"url\".*\})", self.text(r), re.S)
        return json.loads(m.group(1)) if m else None

    async def js(self, page, fn):
        r = await self.call("evaluate_script", {"pageId": page, "function": fn})
        m = re.search(r"(\{.*\})", self.text(r), re.S)
        try:
            return json.loads(m.group(1)) if m else {}
        except Exception:
            return {}

    async def screenshot(self, page):
        r = await self.call("take_screenshot", {"pageId": page})
        for c in r.get("content", []):
            if c.get("type") == "image":
                return base64.b64decode(c["data"])
        m = re.search(r"(/\S+\.png)", self.text(r))   # saved to a file instead
        if m and os.path.exists(m.group(1)):
            return open(m.group(1), "rb").read()
        return None

    async def click(self, page, p):
        await self.call("click_at", {"pageId": page, "x": p[0], "y": p[1]})
        await asyncio.sleep(0.3)

    async def solve(self, page, st):
        """Runs the GLM loop on page. Returns the line added to the result."""
        hit = solver.blocked(st["url"])
        if hit:
            log("blocklisted", st["url"])
            return (f"[captcha-proxy] A CAPTCHA is showing on {st['url']}, which is on "
                    f"the blocklist ({hit}; CAPTCHA_BLOCKLIST or captcha-solve.py): it was left alone. "
                    "Route around it or do without.")
        log("solving", page, st)
        loop = asyncio.get_running_loop()
        errors, last_verify = 0, None
        for rnd in range(1, MAX_ROUNDS + 1):
            png = await self.screenshot(page)
            if not png:
                return "[captcha-proxy] A CAPTCHA is showing, but no screenshot came back."
            try:
                d = await loop.run_in_executor(None, solver.decide, png)
            except solver.SolveError as e:
                log("solver error", e)
                errors += 1
                if errors >= 3 or "OPENROUTER_API_KEY" in str(e):
                    return f"[captcha-proxy] A CAPTCHA is showing; the solver failed: {e}"
                continue          # a malformed answer: look again
            log("round", rnd, json.dumps(d))
            for p in d["points"]:
                await self.click(page, p)
            last_verify = d["verify_point"] or last_verify   # GLM sometimes omits it
            if d["then"] == "verify" and last_verify:
                await self.click(page, last_verify)
                await asyncio.sleep(2.5)
            elif d["then"] == "screenshot":
                await asyncio.sleep(4 if d["points"] else 2)
            elif d["then"] == "cannot" and d["reload_point"]:
                await self.click(page, d["reload_point"])
                await asyncio.sleep(2)
            st = await self.state(page, wait=True) or {}
            if st.get("challenge"):
                continue
            tok = (await self.js(page, TOKEN_JS)).get("token")
            if tok:
                log("solved", page, rnd)
                return (f"[captcha-proxy] A CAPTCHA appeared and was solved automatically "
                        f"({rnd} round{'s' if rnd > 1 else ''}, GLM). Take a new snapshot: "
                        "the page has moved on since this result.")
            if tok == "":
                if st.get("checkbox"):
                    continue          # an unticked checkbox: next round ticks it
                # Rejected and hidden by Google: bring the challenge back.
                log("rejected, reopening", page)
                await self.js(page, REOPEN_JS)
                await asyncio.sleep(1.5)
                continue
            if not st.get("checkbox") or (page, st.get("url")) in self.checkbox_done:
                log("gone (no reCAPTCHA API to confirm)", page, rnd)
                return (f"[captcha-proxy] A CAPTCHA appeared and is gone after {rnd} "
                        "automatic GLM round(s). Take a new snapshot.")
            if d["then"] == "screenshot" and len(d["points"]) == 1:
                self.checkbox_done.add((page, st.get("url")))
        log("gave up", page)
        return (f"[captcha-proxy] A CAPTCHA appeared; automatic solving gave up after "
                f"{MAX_ROUNDS} rounds. It is still on the page.")

    async def handle(self, msg):
        rid = msg["id"]
        name = msg["params"].get("name")
        args = msg["params"].get("arguments") or {}
        fut = asyncio.get_running_loop().create_future()
        self.held[rid] = fut
        await self.write_child(msg)
        resp = await fut
        try:
            res = resp.get("result")
            if res is not None and not res.get("isError"):
                page = args.get("pageId")
                if page is None:   # new_page: the new page is the selected one
                    m = re.search(r"^(\d+): .*\[selected\]", self.text(res), re.M)
                    page = int(m.group(1)) if m else None
                if page is not None:
                    st = await self.state(page, wait=WATCH[name])
                    if st and (st["challenge"] or (
                            st["checkbox"] and (page, st["url"]) not in self.checkbox_done)):
                        note = await self.solve(page, st)
                        res.setdefault("content", []).append({"type": "text", "text": note})
        except Exception as e:   # never break the agent's tool call
            log("error", repr(e))
        await self.write_out(resp)

    async def from_agent(self):
        reader = asyncio.StreamReader()
        await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while line := await reader.readline():
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("method") == "tools/call" and "id" in msg and \
                    msg.get("params", {}).get("name") in WATCH:
                asyncio.create_task(self.handle(msg))
            else:
                await self.write_child(msg)
        self.child.stdin.close()

    async def from_child(self):
        while line := await self.child.stdout.readline():
            try:
                msg = json.loads(line)
            except Exception:
                continue
            rid = msg.get("id")
            if "method" not in msg and rid in self.pending:
                self.pending.pop(rid).set_result(msg)
            elif "method" not in msg and rid in self.held:
                self.held.pop(rid).set_result(msg)
            else:
                await self.write_out(msg)

    async def run(self):
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        self.child = await asyncio.create_subprocess_exec(
            *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024, env=env)
        await asyncio.gather(self.from_agent(), self.from_child())
        await self.child.wait()


if __name__ == "__main__":
    asyncio.run(Proxy(sys.argv[1:]).run())

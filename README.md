# captcha-proxy

![Claude asks Chrome to sign in; the website answers with a CAPTCHA; Chrome sends a screenshot to GLM and gets the clicks back; Claude gets success](docs/flow.png)

Don't stress your Claude with CAPTCHAs. captcha-proxy sends every CAPTCHA your Claude's browser hits to GLM, or any other vision model on OpenRouter, and clicks the answer, so your Claude gets a clean sign-in.

## Setup

1. **Get an OpenRouter key** at https://openrouter.ai/settings/keys and add a few dollars of credit (expect $0.3 per 100 captchas). Set it in your shell profile:
   ```sh
   export OPENROUTER_API_KEY=sk-or-...
   ```
2. **Choose site blocklist** The proxy won't solve on sites in `CAPTCHA_BLOCKLIST`. Set it in your shell profile:
   ```sh
   export CAPTCHA_BLOCKLIST="default, example.com, login.example.org"   # built-in list of 81 sites plus 2 sites
   export CAPTCHA_BLOCKLIST="accounts.google.com, slack.com"            # only these 2 sites
   export CAPTCHA_BLOCKLIST=""                                          # no blocklist
   ```
3. **Let Claude Code install the rest.** Paste this prompt:
   ```text
   Set up captcha-proxy for your browser. Clone https://github.com/curusarn/captcha-proxy
   to ~/git/captcha-proxy and install chrome-devtools-mcp (npm). Register a user-scope MCP
   server named chrome-devtools that runs
     python3 ~/git/captcha-proxy/captcha-proxy.py chrome-devtools-mcp --headless --experimentalVision
       --chromeArg="--user-agent=<a normal, non-headless Chrome user agent for my installed Chrome version>"
   with OPENROUTER_API_KEY and CAPTCHA_BLOCKLIST (if set) passed through from my environment.
   Then run
     python3 ~/git/captcha-proxy/selftest.py <that same command>
   and show me its RESULT line. It should say PASS.
   ```
   Restart Claude Code. That's all: the proxy is now Claude's browser tool, so Claude uses it without being told. A CAPTCHA is solved before the tool result reaches Claude, which only sees one added line saying so.

## How it works

A stdio MCP proxy in front of [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp). After each click, key press or page load it looks for a visible reCAPTCHA or hCaptcha challenge, or an unticked "I'm not a robot" checkbox. When it finds one, it screenshots it, asks the model which tiles to click, clicks them and presses VERIFY, repeating until the CAPTCHA passes. Only then does the tool result go back to Claude.

- `captcha-proxy.py`: the proxy and the solve loop (screenshot, decide, `click_at`,
  VERIFY, repeat). It counts success by reCAPTCHA's own token, and re-opens a
  challenge that Google rejected and hid.
- `captcha-solve.py`: one round, which sends the screenshot to `z-ai/glm-5.3-flashx`
  and turns the answer into clicks. It also holds `BLOCKLIST`, the sites the proxy
  never solves on. It works as a CLI too: `captcha-solve.py --url <page> <screenshot.png>`.

## Options

- `CAPTCHA_MODEL`: any vision model on OpenRouter (default `z-ai/glm-5.3-flashx`).
- If something doesn't work: the log is `~/.cache/captcha-proxy.log`, and `python3 selftest.py <browser MCP command>` checks the setup on Google's reCAPTCHA demo page.

Use it for an agent's normal tasks with your own accounts, never for bulk sign-ups,
scraping or getting around a site's limits. Most sites' terms forbid getting past
their bot checks, and Google and Microsoft lock accounts that sign in through
automation. That is why their sign-ins are on the blocklist.

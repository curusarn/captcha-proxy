# captcha-proxy

Lets a browser agent get past CAPTCHAs without knowing about them. A stdio MCP
proxy in front of [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp):
after each click, key press or page load it looks for a visible reCAPTCHA or
hCaptcha challenge, or an unticked "I'm not a robot" checkbox. When it finds one,
it solves it with a vision GLM model through OpenRouter before the tool result
goes back. The result gets one added line saying what happened.

- `captcha-proxy.py`: the proxy and the solve loop (screenshot, decide, `click_at`,
  VERIFY, repeat). It counts success by reCAPTCHA's own token, and re-opens a
  challenge that Google rejected and hid.
- `captcha-solve.py`: one round, which sends the screenshot to `z-ai/glm-5.3-flashx`
  and turns the answer into clicks. It also holds `BLOCKLIST`, the sites the proxy
  never solves on. It works as a CLI too: `captcha-solve.py --url <page> <screenshot.png>`.

## Use

```sh
OPENROUTER_API_KEY=sk-or-... python3 captcha-proxy.py \
  chrome-devtools-mcp --headless --experimentalVision \
  --chromeArg="--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
```

- `--experimentalVision` is required: it provides the `click_at` tool that the proxy clicks with.
- The plain, non-headless user agent is optional but matters: with the headless one,
  Google challenges far more often and rejects more answers.
- `OPENROUTER_API_KEY` stays in the proxy; the MCP and Chrome don't get it.
- Optional settings: `CAPTCHA_MODEL` (default `z-ai/glm-5.3-flashx`), `CAPTCHA_EFFORT`
  (reasoning effort, default `minimal`, about 4 s a round).
- Log: `~/.cache/captcha-proxy.log`.

Python 3 standard library only.

Use it for an agent's normal tasks with your own accounts, never for bulk sign-ups,
scraping or getting around a site's limits. Most sites' terms forbid getting past
their bot checks, and Google and Microsoft lock accounts that sign in through
automation. That is why their sign-ins are on the blocklist.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Fleet NOC: a live dashboard for a mixed fleet (NVIDIA GPU boxes, Apple Silicon Macs, Raspberry Pis) with a
SwiftUI iPhone app. `README.md` covers features, deployment, configuration and the API; read it first.

## Layout and rules

- **Push model.** `noc_agent.py` on each host POSTs a JSON sample to `/report` every 3 s; the collector keeps the
  latest sample plus ~5 min of history **in memory only** (a restart clears sparklines) and serves `/metrics`.
- **`noc_agent.py` and `noc_server.py` are stdlib-only** and must run on a bare `python3`, including macOS's
  system **Python 3.9**, because agents install onto Pis, DGX boxes and Macs with no pip step. Keep new imports
  out of them and avoid 3.10+ syntax there (check with `/usr/bin/python3`). Only the production collector
  (`noc_web.py` + `noc_auth.py`) needs packages: `deploy/requirements.txt`.
- **`PAGE` in `noc_server.py` is the single source of the dashboard markup**; `noc_web.py` imports it. UI edits
  mean editing that string and restarting the collector. `HIST`/`WIN` are declared separately in both
  collectors, so change them together.
- **The JS lives inside Python strings** (`PAGE`, plus the login and admin pages in `noc_web.py`), so
  `ast.parse` cannot check it. Run the `node --check` snippet in README → *Validating changes*.
- **SVG presentation attributes don't resolve `var()`**: colours in `stop-color`/`stroke` must be literal hex
  (`cvar()` resolves theme colours at render time); text uses `fill="currentColor"`.
- **Topology is computed server-side** (`_topology()` in `noc_web.py`) so the web page and the iPhone app draw the
  same thing. A JSON field rename has to land in `noc_web.py`, its admin-page JS and `ios/NOC/Models.swift`.
- **Agent payloads vary by version**; both UIs decode every field leniently, so one odd host never blanks the
  fleet. Keep it that way.
- **Power control** is password-gated per action, never stored, and audit-logged; commands ride back on the
  agent's next `/report` response (`CMD_TTL` 25 s). Don't add anything that persists or logs the password.
- **ASUS router calls are read-only** (`appGet.cgi` GETs). Never add `apply.cgi`/`applyapp.cgi`.
- **macOS gotchas** already handled in the agent: `ping -W` is milliseconds on macOS but seconds on Linux;
  desktops can publish `SystemPowerIn=0` (treated as absent); M5-class IOReport reports only the GPU rail, so
  power falls back to the SMC total from the `fan` helper.

## Working on it

```bash
pip install -r deploy/requirements.txt
python3 demo/demo.py --port 8788      # real collector + synthetic fleet; prints a sign-in link and session token
python3 noc_agent.py --once           # one sample from this machine
```

iPhone app: `cd ios && xcodegen generate` after any `project.yml` change (the `.xcodeproj` is not committed).
UI tests run against the demo: `TEST_RUNNER_NOC_TOKEN=<session> TEST_RUNNER_NOC_BASE=http://127.0.0.1:8788
xcodebuild test -project NOC.xcodeproj -scheme NOC -destination 'platform=iOS Simulator,name=…'`.

Screenshots in `docs/screenshots/` come from `demo/demo.py` only. Never commit screenshots, hostnames, IPs,
emails, tokens, signing team IDs or device identifiers from a real fleet; `.env`, `*.db` and `power-audit.log`
are git-ignored.

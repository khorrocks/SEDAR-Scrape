# SEDAR-Scrape

Programmatic, stealth automation of **[SEDAR+](https://www.sedarplus.ca/)** — the
Canadian Securities Administrators' public securities-filing system — to:

1. **Enumerate** reporting issuers / companies (the Profiles search), and
2. **Search and bulk-download** all documents filed for a given profile.

The data itself is public regulatory filing data. This tool just automates the
clicks you would otherwise do by hand.

> ⚠️ **Read this first.**
> - SEDAR+ is fronted by **Radware / ShieldSquare bot detection**. Plain headless
>   Selenium gets redirected to a `validate.perfdrive.com` block page. The only
>   approach that works is driving a **real, non-headless Chrome** via
>   `undetected-chromedriver` (under Xvfb on a server).
> - This is **cat-and-mouse**: Radware updates, and any stealth approach can break
>   without notice. Expect to maintain it.
> - Be **polite**: low concurrency, pauses between pages, no hammering. The SEDAR+
>   CDN is slow by design. Automated access may run against the SEDAR+ terms of
>   use — confirm your use case before running at scale.

## How SEDAR+ actually works (and why this is browser-driven)

SEDAR+ is a **stateful, token-driven server app**, not a REST/JSON API:

- A `profile.html?id=<hash>` link immediately redirects into a session-scoped
  `viewInstance/view.html?id=<different-hash>&_timestamp=...`.
- Actions ("Search and download documents", "Download documents") are JS-driven
  server actions tied to opaque session/version identifiers — there are no clean
  endpoints to call directly.

So everything goes through a browser. There is no lightweight `requests` path.

### The document download flow (verified against the live site)

```
profile.html?id=<hash>
  → redirects into a session "View Issuer Profile"
  → click "Search and download documents for this profile"
  → click "Search"                         → paginated results table
  → tick "All documents listed on this page"
  → click "Download documents"             → confirmation MODAL appears:
        "You are downloading N documents, X MB ..."
  → click the modal's green "Download"     → server prepares a zip → download
```

Note the **two-step download**: the blue *Download documents* button only opens the
modal; the modal's green *Download* button is the real trigger.

> 💡 **Popups must be allowed.** The green *Download* fires the download via a
> `window.open()` popup. With Chrome's popup blocker on, the button silently does
> nothing. This tool launches Chrome with `--disable-popup-blocking` and a
> browser-wide download path so the popup's download is captured automatically.

### Enumeration (the Profiles tab)

The Profiles search filters by **Profile type** — `Company`, `Investment fund`,
`Investment fund group`, `Industry participant`, `Third party filer` — and paginates
(30/page). Each row gives **Name, Principal jurisdiction, Type, Number**.

- The CSV **Export** on this page is capped at ~2,030 rows, so we page through the
  **HTML** results instead (uncapped; the UI displays up to 10,000).
- Result rows do **not** contain the opaque `profile.html?id=` URL as a plain href
  — that link comes from the per-row **"Generate URL"** action. But the **Number**
  (e.g. `000003771`) is stable and can be fed straight into the Documents tab's
  *"Profile name or number"* lookup, so you usually don't need the opaque id.

## Install

```bash
pip install -r requirements.txt
# undetected-chromedriver only ships an sdist; if the wheel build fails on a very
# new setuptools, use:  pip install --no-build-isolation undetected-chromedriver
```

You also need a Chrome/Chromium browser. `undetected-chromedriver` will download a
matching driver automatically; on a server, install Xvfb (`apt-get install xvfb`).

## Usage

```bash
# Enumerate every company into a CSV
xvfb-run -a -s "-screen 0 1920x1400x24" \
  python -m sedar.cli enumerate --type Company --out companies.csv

# Download all documents for one profile (by its profile.html id)
xvfb-run -a -s "-screen 0 1920x1400x24" \
  python -m sedar.cli documents \
  --profile-id 517042d52d6b1ddfa40ea23cc4c62739 --out-dir downloads/
```

Useful flags: `--chrome-binary`, `--chromedriver`, `--chrome-version 150`,
`--max-pages`, `--ignore-cert-errors` (only for TLS-intercepting CI sandboxes).

## Verification status

What has been confirmed against the live site (June 2026), driving real Chrome via
`undetected-chromedriver` under Xvfb:

- ✅ Gets past the Radware bot wall (no perfdrive block-page redirect).
- ✅ Profile → "Search and download documents" → results table (e.g. 558 docs).
- ✅ Profiles search with type filter, result counts, and pagination scraping.
- ✅ "All documents listed on this page" select-all + the two-step download modal,
  which correctly reports the selected document count and total size.
- ✅ **Full download:** with popups enabled, the modal's *Download* opens the popup
  and a `requested_documents.zip` lands on disk containing the actual filing PDFs
  (verified: a Homerun Resources news-release PDF).

## Legal / ethical

SEDAR+ data is public regulatory information. Use this responsibly, at low volume,
and in line with SEDAR+'s terms of use. This project is for lawful access to public
filings — not for overwhelming the service.

MIT-licensed. Not affiliated with the CSA or SEDAR+.

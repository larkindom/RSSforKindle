# Kindle Morning Digest

Pulls articles from RSS feeds (and non-RSS pages via CSS selectors), extracts the
full article text, merges multi-page articles into one, and bundles everything
into a single EPUB "periodical" that's emailed to your Kindle every morning.

Runs free in the cloud on GitHub Actions — your Mac doesn't need to be on.

## How it works

```
config.yaml ──> build_digest.py ──> digest.epub ──> email to @kindle.com
   sites           fetch + extract       one file        every morning
```

- **RSS feeds** are read with `feedparser`; only articles newer than
  `lookback_hours` are kept.
- **Non-RSS sites** are scraped: give an `index` URL and a `link_selector`
  (CSS) pointing at the article links.
- **Full text** is extracted with `trafilatura` (strips nav/ads/boilerplate).
- **Paginated articles** are stitched together by following
  `next_page_selector` and concatenating the pages.
- Output is one EPUB with a **section per source**, which Kindle shows as a
  browsable, paginated document.

## Setup

### 1. Configure your sites
Edit [`config.yaml`](config.yaml). Replace the example sources with the sites you
want. Most sites have a feed — try `https://SITE/feed`, `/rss`, or `/atom.xml`.
For sites without one, use the `index` + `link_selector` form.

### 2. Test locally (optional but recommended)
```bash
cd kindle-digest
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python build_digest.py --config config.yaml --out digest.epub   # build only, no email
```
Open `digest.epub` to check it looks right. To test emailing too:
```bash
cp .env.example .env   # fill it in, then:
set -a; source .env; set +a
python build_digest.py --send
```

### 3. Put it on GitHub Actions
1. Create a new GitHub repo and push this folder to it.
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, and add: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`,
   `MAIL_FROM`, `KINDLE_EMAIL` (see [`.env.example`](.env.example) for what each is).
3. The job is scheduled in [`.github/workflows/digest.yml`](.github/workflows/digest.yml)
   for 11:00 UTC (≈7 AM US Eastern). Change the `cron` line to your time.
4. Test it now: **Actions → Morning Kindle Digest → Run workflow**.

### 4. Amazon side (one-time)
- Find your Send-to-Kindle address: Amazon → **Manage Your Content and Devices →
  Preferences → Personal Document Settings**.
- On that same page, add your `MAIL_FROM` address to **Approved Personal Document
  E-mail List**, or Amazon will silently reject the email.

## Paywalls via archive.today

When an article's directly-fetched text comes back shorter than
`min_content_chars` (i.e. it looks like a paywall teaser), the script
automatically looks up the newest **archive.today** snapshot
(`archive.ph/newest/<url>`) and uses that instead. For sites you know are always
paywalled, set `archive: always` on the source to skip the direct fetch.

```yaml
- name: The Wall Street Journal
  feed: https://feeds.content.dowjones.io/public/rss/RSSWorldNews
  archive: always
```

**Important reliability caveat:** archive.today sits behind Cloudflare and
rate-limits automated traffic (HTTP 429). It works most reliably from a home/
residential IP and is throttled harder from datacenter IPs like GitHub Actions
runners. The script retries with backoff and only reads snapshots that already
exist (it does not create new ones — that needs a browser/captcha). Expect it to
succeed for many but not necessarily all paywalled articles on a given morning.
If archive coverage matters a lot to you, the more robust escalation is to run
the job from your Mac (residential IP) or add a headless-browser step — ask and
I'll wire that up.

## Adding a tricky site

Multi-page article that should be merged:
```yaml
- name: Ars Technica
  feed: https://feeds.arstechnica.com/arstechnica/index
  next_page_selector: "nav.page-numbers a.next"
```

Site with no feed at all:
```yaml
- name: Example Blog
  index: https://example.com/blog
  link_selector: "article h2 a"
  max_articles: 5
```

Filter by keyword:
```yaml
- name: TechCrunch
  feed: https://techcrunch.com/feed/
  include: ["AI", "startup"]
  exclude: ["sponsored"]
```

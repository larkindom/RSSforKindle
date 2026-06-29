#!/usr/bin/env python3
"""
Build a single Kindle-ready EPUB "periodical" from a list of RSS feeds and/or
scraped index pages, then (optionally) email it to a Send-to-Kindle address.

Design goals matching the brief:
  * "capture certain sites"      -> RSS feeds OR CSS-selector scraping of pages
  * "paginated articles"         -> follow next_page_selector and merge pages
  * "one single feed each morning" -> one EPUB with a section per source, run by cron

Usage:
  python build_digest.py --config config.yaml --out digest.epub          # build only
  python build_digest.py --config config.yaml --out digest.epub --send   # build + email

Email settings are read from environment variables (set as GitHub secrets):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, KINDLE_EMAIL
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
import trafilatura
import yaml
from dateutil import parser as dateparser
from ebooklib import epub
from lxml import html as lxml_html
from zoneinfo import ZoneInfo

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 KindleDigest/1.0"
)
HTTP = requests.Session()
HTTP.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
)
REQUEST_TIMEOUT = 25
ARCHIVE_HOSTS = ("archive.ph", "archive.today", "archive.is", "archive.li")


# ──────────────────────────────── helpers ──────────────────────────────────

def log(msg: str) -> None:
    print(f"[digest] {msg}", file=sys.stderr, flush=True)


def http_get(url: str, retries: int = 1) -> requests.Response | None:
    """GET with optional retry/backoff. archive.today hosts get extra retries
    because they rate-limit (HTTP 429) automated traffic via Cloudflare.
    """
    if any(h in url for h in ARCHIVE_HOSTS):
        retries = max(retries, 2)
    last_exc = None
    for attempt in range(retries):
        try:
            r = HTTP.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if r.status_code in (429, 503) and attempt < retries - 1:
                wait = 2 * (attempt + 1)
                log(f"    {r.status_code} from host, retrying in {wait}s")
                time.sleep(wait)
                continue
            return r
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    if last_exc:
        log(f"  ! fetch failed {url}: {last_exc}")
    return None


def fetch(url: str) -> str | None:
    r = http_get(url)
    if r is None:
        return None
    try:
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 — best-effort fetch, log and skip
        log(f"  ! fetch failed {url}: {e}")
        return None
    return r.text


def parse_date(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        d = dateparser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def matches_filters(title: str, include, exclude) -> bool:
    t = (title or "").lower()
    if include and not any(k.lower() in t for k in include):
        return False
    if exclude and any(k.lower() in t for k in exclude):
        return False
    return True


def extract_content(raw: str) -> str | None:
    """Extract the article body as simple HTML, robust to trafilatura quirks.

    Tries HTML output first (keeps headings/lists); if that path errors on a
    given document, falls back to plain-text output wrapped in paragraphs.
    """
    try:
        content = trafilatura.extract(
            raw,
            output_format="html",
            include_links=False,
            include_images=False,
            favor_recall=True,
        )
        if content:
            return content
    except Exception as e:  # noqa: BLE001 — trafilatura html path is fragile
        log(f"    (html extract failed, falling back to text: {e})")

    try:
        text = trafilatura.extract(raw, include_links=False, include_images=False)
    except Exception:  # noqa: BLE001
        text = None
    if not text:
        return None
    paras = [f"<p>{html.escape(p.strip())}</p>" for p in text.split("\n") if p.strip()]
    return "\n".join(paras) if paras else None


def text_length(content: str | None) -> int:
    """Rough count of visible text characters, used to spot truncated articles."""
    if not content:
        return 0
    return len(re.sub(r"<[^>]+>", "", content).strip())


# archive.today mirrors — same index, tried in order if one is unreachable.
ARCHIVE_MIRRORS = ["https://archive.ph", "https://archive.today", "https://archive.is"]
# Safety valve: cap how many archive.today lookups one run may attempt, so a
# feed full of blocked/thin links can't make the morning job hang.
MAX_ARCHIVE_LOOKUPS = 25
_archive_lookups_used = 0


def archive_lookup(url: str) -> str | None:
    """Return the newest archive.today snapshot URL for `url`, or None.

    Hits <mirror>/newest/<url>, which redirects to the latest saved snapshot
    when one exists. We do NOT trigger new captures (those need a browser /
    captcha); we only read snapshots that already exist.
    """
    global _archive_lookups_used
    if _archive_lookups_used >= MAX_ARCHIVE_LOOKUPS:
        log("    archive lookup budget exhausted for this run")
        return None
    _archive_lookups_used += 1
    for base in ARCHIVE_MIRRORS:
        r = http_get(f"{base}/newest/{url}")
        if r is None:
            continue
        final = r.url
        # A hit redirects away from /newest/ to a snapshot URL, either a short
        # code (archive.ph/AbCdE) or a timestamped capture
        # (archive.ph/20240101000000/http://...). A miss stays on /newest/.
        path = urlsplit(final).path
        is_snapshot = bool(
            re.match(r"^/\w{4,8}$", path) or re.match(r"^/\d{4,14}/", path)
        )
        if is_snapshot and r.status_code == 200:
            log(f"    archive snapshot: {final}")
            return final
        if is_snapshot and r.status_code in (429, 403):
            # We know a snapshot exists but Cloudflare blocked the body. Return
            # the canonical snapshot URL anyway; the content fetch retries it.
            log(f"    archive snapshot found but host throttled ({r.status_code})")
            return final
    log("    no usable archive.today snapshot")
    return None


def extract_article(url: str, src: dict, digest_cfg: dict) -> str | None:
    """Extract an article, using archive.today for paywalled / thin content.

    archive mode (per-source `archive:` key): "always" | "fallback" | "never".
    In "fallback" (default) we fetch directly first and only reach for the
    archive when the result looks truncated (shorter than min_content_chars).
    """
    next_page_selector = src.get("next_page_selector")
    archive_mode = str(src.get("archive", "fallback")).lower()
    min_chars = digest_cfg.get("min_content_chars", 600)

    # Sites you know are always paywalled: go straight to the archive.
    if archive_mode == "always":
        snap = archive_lookup(url)
        if snap:
            content = extract_via_url(snap, next_page_selector)
            if text_length(content) >= min_chars:
                return content
            log("    archive content thin too; falling back to direct fetch")

    content = extract_via_url(url, next_page_selector)
    direct_len = text_length(content)

    # Auto-fallback: thin direct content usually means a paywall snippet.
    if archive_mode != "never" and direct_len < min_chars:
        log(f"    direct content thin ({direct_len} chars) — trying archive.today")
        snap = archive_lookup(url)
        if snap:
            archived = extract_via_url(snap, next_page_selector)
            if text_length(archived) > direct_len:
                return archived

    return content


def extract_via_url(url: str, next_page_selector: str | None) -> str | None:
    """Fetch a URL, extract main content, and follow pagination if configured.

    Returns sanitized HTML (paragraphs) ready to drop into an EPUB chapter,
    or None if nothing usable could be extracted.
    """
    pages_html: list[str] = []
    seen: set[str] = set()
    current = url

    for _ in range(12):  # generous hard cap on pages followed
        if not current or current in seen:
            break
        seen.add(current)

        raw = fetch(current)
        if raw is None:
            break

        # trafilatura is excellent at isolating the real article body and
        # stripping nav/ads/boilerplate.
        content = extract_content(raw)
        if content:
            pages_html.append(content)

        if not next_page_selector:
            break

        # Find the "next page" link to merge multi-page articles into one.
        try:
            tree = lxml_html.fromstring(raw)
            nxt = tree.cssselect(next_page_selector)
        except Exception:  # noqa: BLE001
            nxt = []
        if not nxt:
            break
        href = nxt[0].get("href")
        if not href:
            break
        current = urljoin(current, href)

    if not pages_html:
        return None
    return "\n<hr/>\n".join(pages_html)


# ──────────────────────────── source collection ────────────────────────────

def collect_from_feed(src: dict, cutoff: dt.datetime) -> list[dict]:
    feed_url = src["feed"]
    max_articles = src.get("max_articles", 20)
    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        log(f"  ! feed error {feed_url}: {getattr(parsed, 'bozo_exception', '')}")
        return []

    out: list[dict] = []
    for entry in parsed.entries:
        title = html.unescape(entry.get("title", "(untitled)"))
        link = entry.get("link")
        if not link:
            continue
        if not matches_filters(title, src.get("include"), src.get("exclude")):
            continue

        published = parse_date(entry.get("published") or entry.get("updated"))
        if published and published < cutoff:
            continue

        out.append({"title": title, "link": link, "published": published})
        if len(out) >= max_articles:
            break
    return out


def collect_from_index(src: dict) -> list[dict]:
    index_url = src["index"]
    selector = src["link_selector"]
    max_articles = src.get("max_articles", 10)

    raw = fetch(index_url)
    if raw is None:
        return []
    try:
        tree = lxml_html.fromstring(raw)
    except Exception as e:  # noqa: BLE001
        log(f"  ! parse failed {index_url}: {e}")
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for a in tree.cssselect(selector):
        href = a.get("href")
        if not href:
            continue
        link = urljoin(index_url, href)
        if link in seen:
            continue
        seen.add(link)
        title = html.unescape((a.text_content() or "").strip()) or link
        if not matches_filters(title, src.get("include"), src.get("exclude")):
            continue
        out.append({"title": title, "link": link, "published": None})
        if len(out) >= max_articles:
            break
    return out


# ───────────────────────────── EPUB assembly ───────────────────────────────

def build_epub(digest_cfg: dict, sections: list[dict], out_path: str) -> None:
    tz = ZoneInfo(digest_cfg.get("timezone", "UTC"))
    today = dt.datetime.now(tz).strftime("%A, %B %-d, %Y")
    base_title = digest_cfg.get("title", "Morning Digest")

    book = epub.EpubBook()
    # Stable identifier + constant title so each morning's delivery is treated as
    # an UPDATE to the same Kindle document (replacing yesterday's) instead of a
    # brand-new library entry. The date is shown on the cover page inside, so you
    # can still tell which issue you're reading. This keeps the Library tidy.
    book.set_identifier("kindle-morning-digest")
    book.set_title(base_title)
    book.set_language("en")
    book.add_author("Kindle Digest")

    css = epub.EpubItem(
        uid="style",
        file_name="style/main.css",
        media_type="text/css",
        content=(
            "body{font-family:Georgia,serif;line-height:1.5;}"
            "h1{font-size:1.5em;} h2{font-size:1.2em;}"
            "p{margin:0 0 1em 0;} hr{border:0;border-top:1px solid #ccc;}"
            ".src{color:#555;font-size:0.85em;}"
        ),
    )
    book.add_item(css)

    # Cover page showing the issue date (the document title itself stays
    # constant for the in-place-update behavior described above).
    cover = epub.EpubHtml(title=base_title, file_name="cover.xhtml", lang="en")
    cover.content = (
        f"<h1>{html.escape(base_title)}</h1>"
        f"<p class='src'>{html.escape(today)}</p>"
    )
    cover.add_item(css)
    book.add_item(cover)

    chapters: list[epub.EpubHtml] = []
    toc_sections: list = []
    spine: list = ["nav", cover]

    for s_idx, section in enumerate(sections):
        section_chapters: list[epub.EpubHtml] = []
        for a_idx, art in enumerate(section["articles"]):
            fname = f"s{s_idx}_a{a_idx}.xhtml"
            pub = art.get("published")
            pub_str = pub.astimezone(tz).strftime("%b %-d, %-I:%M %p") if pub else ""
            body = (
                f"<h1>{html.escape(art['title'])}</h1>"
                f"<p class='src'>{html.escape(section['name'])}"
                f"{' · ' + pub_str if pub_str else ''} · "
                f"<a href='{html.escape(art['link'])}'>source</a></p>"
                f"{art['content']}"
            )
            ch = epub.EpubHtml(title=art["title"], file_name=fname, lang="en")
            ch.content = body
            ch.add_item(css)
            book.add_item(ch)
            chapters.append(ch)
            section_chapters.append(ch)
            spine.append(ch)

        if section_chapters:
            toc_sections.append(
                (epub.Section(section["name"]), tuple(section_chapters))
            )

    book.toc = tuple(toc_sections)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(out_path, book)
    log(f"wrote {out_path} ({len(chapters)} articles across {len(toc_sections)} sections)")


# ──────────────────────────────── email ────────────────────────────────────

def send_to_kindle(epub_path: str, digest_title: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    mail_from = os.environ.get("MAIL_FROM", user)
    kindle = os.environ["KINDLE_EMAIL"]

    msg = EmailMessage()
    msg["Subject"] = digest_title  # Kindle uses subject as the document title
    msg["From"] = mail_from
    msg["To"] = kindle
    msg.set_content("Your morning digest is attached.")

    with open(epub_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="epub+zip",
            filename=os.path.basename(epub_path),
        )

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    # Mask the address so it isn't exposed in this public repo's Action logs.
    masked = re.sub(r"^(.).*(@.*)$", r"\1***\2", kindle)
    log(f"emailed digest to {masked}")


# ──────────────────────────────── main ─────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Build a Kindle morning digest EPUB.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="digest.epub")
    ap.add_argument("--send", action="store_true", help="email the EPUB to Kindle")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    digest_cfg = cfg.get("digest", {})
    lookback = digest_cfg.get("lookback_hours", 26)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback)
    max_total = digest_cfg.get("max_total_articles", 60)

    sections: list[dict] = []
    total = 0
    for src in cfg.get("sources", []):
        name = src.get("name", "Source")
        log(f"source: {name}")
        if "feed" in src:
            items = collect_from_feed(src, cutoff)
        elif "index" in src and "link_selector" in src:
            items = collect_from_index(src)
        else:
            log(f"  ! skipping {name}: needs either 'feed' or 'index'+'link_selector'")
            continue

        articles: list[dict] = []
        for it in items:
            if total >= max_total:
                break
            log(f"  · {it['title'][:70]}")
            content = extract_article(it["link"], src, digest_cfg)
            if not content:
                log("    (no extractable content, skipped)")
                continue
            it["content"] = content
            articles.append(it)
            total += 1

        if articles:
            sections.append({"name": name, "articles": articles})

    if not sections:
        log("no articles collected — nothing to send")
        return 1

    build_epub(digest_cfg, sections, args.out)

    if args.send:
        # Constant subject (and the constant filename used in send_to_kindle)
        # so Send-to-Kindle updates the same document instead of piling up.
        send_to_kindle(args.out, digest_cfg.get("title", "Morning Digest"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

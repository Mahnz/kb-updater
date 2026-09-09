#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
import textwrap
import time
from collections import namedtuple
from datetime import datetime
from urllib.request import build_opener, HTTPCookieProcessor
from urllib.error import URLError, HTTPError

SUPPORT_BASE = "https://support.trellix.com"
PRINT_URL = SUPPORT_BASE + "/articles/{lang}/{atype}/{url_name}/p"
LOGIN_URL = SUPPORT_BASE + "/s/login/"
DEFAULT_LANG = "en_US"
DEFAULT_SESSION_FILE = ".trellix_session.json"
OLD_DIRNAME = "OLD"
TIMEOUT = 25  # seconds

# Set by --debug; when True, debug() prints to stderr instead of being a no-op.
DEBUG = False


def debug(msg):
    """Print a debug line to stderr, but only under --debug."""
    if DEBUG:
        print(f"[debug] {msg}", file=sys.stderr)

# PDF output settings passed to Playwright's page.pdf() under --download.
# Edit these to taste. Margins accept CSS units: "1cm", "0.5in", "12mm".
PDF_OPTIONS = {
    "format": "A4",  # "A4", "Letter", "Legal", ...
    "landscape": False,
    "scale": 1.0,  # 0.1 - 2.0
    "print_background": True,
    "margin": {"top": "1cm", "right": "1cm", "bottom": "1cm", "left": "1cm"},
}

# Classic-portal article-type segment. Trellix serves all KB articles under this
# one type; change it here if that ever stops being true.
ARTICLE_TYPE = "Best_Practices"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Recognised filename formats. Each pattern must expose three named groups:
#   id   - article id (KB<number> or A<number>)
#   date - saved date (styles accepted are listed in FILE_DATE_FORMATS)
#   name - human-readable article name
# Add a pattern here to support another naming convention; parsing and the
# rebuilt output filename adapt automatically.
FILENAME_PATTERNS = [
    # KB88569_2025.08.07_title.pdf   |   A000013146_20260416_title.pdf
    re.compile(
        r"^(?P<id>KB\d+|A\d+)_(?P<date>\d{4}\.\d{2}\.\d{2}|\d{8})_(?P<name>.+)$"
    ),
    # KB74059 -20250407- this is a kb.pdf   (spaces and dashes)
    re.compile(
        r"^(?P<id>KB\d+|A\d+)\s*-\s*(?P<date>\d{4}\.\d{2}\.\d{2}|\d{8})\s*-\s*(?P<name>.+)$"
    ),
]

# Filename date styles, reused verbatim when writing the updated filename.
FILE_DATE_FORMATS = ("%Y.%m.%d", "%Y%m%d", "%Y-%m-%d")

_DATE = r"(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}\s*[AP]M)?)"
_LAST_MODIFIED_RE = re.compile(r"Last Modified Date\s*:?\s*" + _DATE)
_URL_NAME_RE = re.compile(r"URL Name\s*:?\s*([A-Za-z0-9_]+)")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_WS_RE = re.compile(r"\s+")
_DATE_FORMATS = ("%m/%d/%Y %I:%M %p", "%m/%d/%Y")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


Parsed = namedtuple("Parsed", "filename ident kind file_date name date_span")


def _parse_file_date(token):
    """Parse a filename date token into a date, trying each FILE_DATE_FORMATS."""
    for fmt in FILE_DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    return None


def parse_filename(filename):
    """Match a filename against FILENAME_PATTERNS. Return a Parsed, or None."""
    for pattern in FILENAME_PATTERNS:
        m = pattern.match(filename)
        if not m:
            continue
        ident = m.group("id")
        kind = "kb" if ident.upper().startswith("KB") else "article_number"
        name = os.path.splitext(m.group("name"))[0].strip()
        return Parsed(
            filename,
            ident,
            kind,
            _parse_file_date(m.group("date")),
            name,
            m.span("date"),
        )
    return None


def updated_filename(parsed, new_date):
    """Rebuild parsed.filename with the date updated (same style) and a .pdf suffix."""
    start, end = parsed.date_span
    old_token = parsed.filename[start:end]
    if "." in old_token:
        token = new_date.strftime("%Y.%m.%d")
    elif "-" in old_token:
        token = new_date.strftime("%Y-%m-%d")
    else:
        token = new_date.strftime("%Y%m%d")
    stem = parsed.filename[:start] + token + parsed.filename[end:]
    return os.path.splitext(stem)[0] + ".pdf"


def classic_slug(ident, kind):
    """URL Name used on the printable page: KB number as-is, article number without 'A'."""
    return ident[1:] if kind == "article_number" else ident


def _parse_portal_date(raw):
    """Parse the portal's 'Last Modified Date' string into a date."""
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def extract_last_modified(html):
    """Return (last_modified_date, url_name) from an article page, or (None, None)."""
    try:
        text = _SCRIPT_RE.sub(" ", html or "")
        text = _TAG_RE.sub(" ", text).replace("&nbsp;", " ")
        text = _WS_RE.sub(" ", text)
        lm = _LAST_MODIFIED_RE.search(text)
        if not lm:
            return None, None
        un = _URL_NAME_RE.search(text)
        return _parse_portal_date(lm.group(1)), (un.group(1) if un else None)
    except Exception as exc:  # never raise from parsing
        debug(f"extract_last_modified: parse error: {exc}")
        return None, None


def extract_title(html):
    """Best-effort article title from a classic page (its first <h1>)."""
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html or "")
    if not m:
        return None
    title = _WS_RE.sub(" ", _TAG_RE.sub(" ", m.group(1))).strip()
    return title or None


def slugify(text, maxlen=80):
    """Turn an article title into a filename-friendly hyphenated slug."""
    text = re.sub(r"[^\w\s-]", "", text or "")
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text[:maxlen].strip("-") or "article"


# --------------------------------------------------------------------------- #
# Session cookies (saved by --login) and the stdlib opener
# --------------------------------------------------------------------------- #


Session = namedtuple("Session", "exists valid detail cookies header saved")


def read_session(session_file):
    """Read a saved Playwright storage-state file once and describe it: exists/valid
    flags, a detail string + saved time, the full cookie list (for the browser) and
    a Trellix-only Cookie header (for HTTP requests)."""
    if not session_file or not os.path.exists(session_file):
        return Session(False, False, "no saved session", [], None, None)
    try:
        with open(session_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return Session(True, False, "unreadable session file", [], None, None)
    cookies = (
        data.get("cookies", [])
        if isinstance(data, dict)
        else (data if isinstance(data, list) else [])
    )
    trellix = [c for c in cookies if "trellix.com" in (c.get("domain") or "")]
    pairs = []
    for c in trellix:
        try:
            pairs.append(f"{c['name']}={c['value']}")
        except Exception:
            continue
    header = "; ".join(pairs) or None
    now = time.time()
    live = sum(
        1
        for c in trellix
        if c.get("expires", -1) in (None, -1) or c.get("expires", -1) > now
    )
    if not trellix:
        valid, detail = False, "no Trellix cookies in session"
    elif live == 0:
        valid, detail = False, "session cookies expired"
    else:
        valid, detail = True, f"{live} live cookie(s)"
    return Session(
        True, valid, detail, cookies, header, _session_saved_when(session_file)
    )


def _session_saved_when(session_file):
    """Human-readable time the session file was last written, or None."""
    try:
        ts = os.path.getmtime(session_file)
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return None


def _ask_yes_no(question, default=False):
    """Prompt for yes/no. Returns default on empty answer or no input available."""
    suffix = " [y/N] " if not default else " [Y/n] "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def make_opener(cookie_header):
    """urllib opener with a User-Agent and, if available, a session Cookie header."""
    opener = build_opener(HTTPCookieProcessor())
    headers = [("User-Agent", USER_AGENT)]
    if cookie_header:
        headers.append(("Cookie", cookie_header))
    opener.addheaders = headers
    return opener


def http_get(opener, url):
    """GET a URL and return decoded text, or None on any error."""
    debug(f"GET {url}")
    try:
        with opener.open(url, timeout=TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
            debug(f"  -> {resp.status} {len(body)} bytes")
            return body
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        debug(f"  -> error: {exc}")
        return None


def resolve_classic(opener, slug, lang):
    """Read the printable page for a URL Name (KB number or article number).

    Returns (last_modified, print_url) or (None, None) (empty => login-gated/missing).
    """
    url = PRINT_URL.format(lang=lang, atype=ARTICLE_TYPE, url_name=slug)
    html = http_get(opener, url)
    if not html or not html.strip():
        debug(f"resolve_classic({slug}): empty body (login-gated or missing)")
        return None, None
    last_modified, url_name = extract_last_modified(html)
    if last_modified and url_name and url_name.upper() == slug.upper():
        return last_modified, url
    debug(
        f"resolve_classic({slug}): last_modified={last_modified} "
        f"url_name={url_name} (mismatch or unparsed)"
    )
    return None, None


def fetch_article(opener, ident, kind, lang):
    """For --add: fetch (last_modified, print_url, title) for an id, or None."""
    slug = classic_slug(ident, kind)
    url = PRINT_URL.format(lang=lang, atype=ARTICLE_TYPE, url_name=slug)
    html = http_get(opener, url)
    if not html or not html.strip():
        return None
    last_modified, url_name = extract_last_modified(html)
    if not (last_modified and url_name and url_name.upper() == slug.upper()):
        return None
    return last_modified, url, extract_title(html)


def classic_url(ident, kind, lang):
    """Printable (/p) classic article URL for a KB number or an A<number> id."""
    return PRINT_URL.format(
        lang=lang, atype=ARTICLE_TYPE, url_name=classic_slug(ident, kind)
    )


# --------------------------------------------------------------------------- #
# Report (default) - standard library only, printed once at the end
# --------------------------------------------------------------------------- #


def build_report(directory, articles, opener, lang):
    outdated, uptodate, login = [], [], []
    for a in articles:
        try:
            remote_date, _ = resolve_classic(
                opener, classic_slug(a.ident, a.kind), lang
            )
        except Exception as exc:  # one bad article must not stop the report
            debug(f"build_report({a.ident}): {exc}")
            remote_date = None
        url = classic_url(a.ident, a.kind, lang)
        if remote_date is None:
            login.append((a.ident, url))
        elif a.file_date is None or remote_date > a.file_date:
            outdated.append((a.ident, a.file_date, remote_date, url))
        else:
            uptodate.append(a.ident)

    W = 70
    rule = "-" * W
    out = []
    out.append("=" * W)
    out.append("  Trellix KB updater - report")
    out.append(f"  Folder: {directory}")
    out.append(
        f"  {len(articles)} files | {len(outdated)} out of date"
        f" | {len(uptodate)} up to date | {len(login)} login required"
    )
    out.append("=" * W)

    if outdated:
        out.append("")
        out.append(f"  OUT OF DATE  ({len(outdated)})")
        out.append("  " + rule)
        for ident, file_date, remote_date, url in outdated:
            was = file_date if file_date else "?"
            out.append(f"    {ident:<12} {was}  ->  {remote_date}")
            out.append(f"      {url}")

    if login:
        out.append("")
        out.append(f"  LOGIN REQUIRED  ({len(login)})   run:  python update.py --login")
        out.append("  " + rule)
        for ident, url in login:
            out.append(f"    {ident:<12} {url}")

    if uptodate:
        out.append("")
        out.append(f"  UP TO DATE  ({len(uptodate)})")
        out.append("  " + rule)
        out.append(
            textwrap.fill(
                ", ".join(uptodate),
                width=W,
                initial_indent="    ",
                subsequent_indent="    ",
            )
        )

    out.append("")
    out.append("=" * W)
    if outdated:
        out.append("  Run with --download to save the out-of-date articles as PDF.")
    elif login:
        out.append("  Run --login to check the login-only articles.")
    else:
        out.append("  Everything is up to date.")
    out.append("=" * W)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Playwright - only for --login and --download (PDF rendering)
# --------------------------------------------------------------------------- #


def _load_playwright():
    """Return sync_playwright, or None after printing an install hint."""
    try:
        from playwright.sync_api import sync_playwright

        return sync_playwright
    except Exception:
        print("This needs Playwright. Install it once with:")
        print("    pip install playwright")
        print("    playwright install chromium")
        return None


def _launch_browser(pw, channel, headless):
    """Launch Chromium, optionally via an installed browser channel (msedge/chrome)."""
    if channel:
        return pw.chromium.launch(headless=headless, channel=channel)
    return pw.chromium.launch(headless=headless)


def interactive_login(session_file, channel):
    """Open a real browser for the user to sign in; save the session cookie."""
    sync_playwright = _load_playwright()
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as pw:
            browser = _launch_browser(pw, channel, headless=False)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.goto(LOGIN_URL, timeout=TIMEOUT * 1000)
            print("\nA browser window opened at the Trellix sign-in page.")
            print("Sign in there (your password stays in the browser), then")
            try:
                input("press Enter here to save the session... ")
            except EOFError:
                pass
            context.storage_state(path=session_file)
            browser.close()
    except Exception as exc:
        print(f"! login failed: {exc}")
        return False
    print(f"Session saved to {session_file}. --download and the report reuse it.")
    return True


def render_pdf(page, url, pdf_path):
    """Navigate to url and save it as PDF using PDF_OPTIONS. Returns True on success."""
    debug(f"render_pdf: goto {url}")
    try:
        page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 1000)
    except Exception as exc:
        debug(f"render_pdf: goto failed: {exc}")
        return False
    # Try the configured options; fall back to a minimal call if they are rejected.
    for opts in (PDF_OPTIONS, {"print_background": True}):
        try:
            page.pdf(path=pdf_path, **opts)
            if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                debug(f"render_pdf: wrote {pdf_path} with opts={opts}")
                return True
        except Exception as exc:
            debug(f"render_pdf: pdf() with opts={opts} failed: {exc}")
            continue
    return False


def move_to_old(directory, filename):
    old_dir = os.path.join(directory, OLD_DIRNAME)
    os.makedirs(old_dir, exist_ok=True)
    os.replace(os.path.join(directory, filename), os.path.join(old_dir, filename))


def _log(status, ident, detail=""):
    print(f"  {status:<11} {ident:<12} {detail}".rstrip())


def process_article(directory, opener, page, lang, article):
    """Check one article; if out of date save PDF + archive old. Returns a status."""
    remote_date, print_url = resolve_classic(
        opener, classic_slug(article.ident, article.kind), lang
    )
    if remote_date is None:
        _log("not found", article.ident, classic_url(article.ident, article.kind, lang))
        return "notfound"
    if article.file_date is not None and remote_date <= article.file_date:
        _log("up to date", article.ident, str(article.file_date))
        return "uptodate"

    new_name = updated_filename(article, remote_date)
    pdf_path = os.path.join(directory, new_name)
    if not render_pdf(page, print_url, pdf_path):
        _log("FAILED", article.ident, "could not render PDF")
        return "failed"
    try:
        move_to_old(directory, article.filename)
    except Exception as exc:
        _log(
            "FAILED", article.ident, f"PDF saved but could not archive old file: {exc}"
        )
        return "failed"
    _log(
        "UPDATED", article.ident, f"{article.file_date} -> {remote_date}  ({new_name})"
    )
    return "updated"


def _with_browser(cookies, channel, work):
    """Run work(page) in a headless browser with the saved cookies applied.

    Returns work's result, or None if Playwright or the browser is unavailable.
    """
    sync_playwright = _load_playwright()
    if sync_playwright is None:
        return None
    try:
        with sync_playwright() as pw:
            debug(f"_with_browser: launching chromium (channel={channel})")
            browser = _launch_browser(pw, channel, headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            if cookies:
                try:
                    context.add_cookies(cookies)
                    debug(f"_with_browser: applied {len(cookies)} saved cookie(s)")
                except Exception as e:
                    print(f"! could not apply saved session cookies: {e}")
            page = context.new_page()
            try:
                return work(page)
            finally:
                browser.close()
    except Exception as e:
        print(f"! browser error: {e}")
        print("  If Chromium is missing:  playwright install chromium")
        print("  Or use an installed browser:  --channel msedge")
        return None


def run_download(directory, articles, opener, lang, cookies, channel):
    print(
        "Downloading updates with Playwright"
        + (f" (channel: {channel})" if channel else "")
        + f"\nFolder: {directory}\n"
    )

    def work(page):
        counts = {}
        for article in articles:
            try:
                status = process_article(directory, opener, page, lang, article)
            except Exception as e:  # isolate per-article failures
                _log("ERROR", article.ident, str(e)[:70])
                status = "error"
            counts[status] = counts.get(status, 0) + 1
        return counts

    counts = _with_browser(cookies, channel, work)
    if counts is None:
        return
    parts = [
        f"{counts.get('updated', 0)} updated",
        f"{counts.get('uptodate', 0)} up to date",
        f"{counts.get('notfound', 0)} not found",
    ]
    if counts.get("failed"):
        parts.append(f"{counts['failed']} failed")
    if counts.get("error"):
        parts.append(f"{counts['error']} errors")
    print("\nDone: " + ", ".join(parts) + ".")


def _find_existing(directory, ident):
    """Return the Parsed of an already-saved file with this id, or None."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    for name in names:
        parsed = parse_filename(name)
        if parsed and parsed.ident.upper() == ident.upper():
            return parsed
    return None


def run_add(directory, raw_id, lang, session_file, channel, sess):
    """--add: download a single article by id (KB<n> or A<n>) into the folder.

    --login is never required up front: if the article can't be read (usually
    because it is login-gated), offer to sign in and retry. If a file for the
    article is already in the folder, report whether it is up to date and, when
    out of date, offer to update it (old file moves to OLD/).
    """
    m = re.fullmatch(r"\s*(KB|A)(\d+)\s*", raw_id or "", re.IGNORECASE)
    if not m:
        print(f"! '{raw_id}' is not a valid id (expected KB<number> or A<number>).")
        return
    ident = m.group(1).upper() + m.group(2)
    kind = "kb" if ident.startswith("KB") else "article_number"

    meta = fetch_article(make_opener(sess.header), ident, kind, lang)
    if meta is None:
        print(f"{ident} could not be read - it may be a login-only article.")
        if _ask_yes_no(f"Log in and retry {ident}?"):
            if interactive_login(session_file, channel):
                sess = read_session(session_file)
                meta = fetch_article(make_opener(sess.header), ident, kind, lang)

    if meta is None:
        print(f"  not found   {ident}   {classic_url(ident, kind, lang)}")
        return

    last_modified, print_url, title = meta

    existing = _find_existing(directory, ident)
    if existing is not None:
        print(f"{ident} is already in the folder as {existing.filename}.")
        if existing.file_date is not None and last_modified <= existing.file_date:
            print(f"  up to date ({existing.file_date}).")
            return
        print(f"  out of date ({existing.file_date} -> {last_modified}).")
        if not _ask_yes_no("Update it?"):
            print("  Left unchanged.")
            return
        new_name = updated_filename(existing, last_modified)
    else:
        slug = slugify(title) if title else "article"
        new_name = f"{ident}_{last_modified:%Y.%m.%d}_{slug}.pdf"

    pdf_path = os.path.join(directory, new_name)
    result = _with_browser(
        sess.cookies, channel, lambda page: render_pdf(page, print_url, pdf_path)
    )
    if result is not True:
        if result is False:
            _log("FAILED", ident, "could not render PDF")
        return
    if existing is not None:
        try:
            move_to_old(directory, existing.filename)
        except Exception as exc:
            _log("FAILED", ident, f"PDF saved but could not archive old file: {exc}")
            return
        _log("UPDATED", ident, f"{existing.file_date} -> {last_modified}  ({new_name})")
    else:
        _log("ADDED", ident, f"{last_modified}  ({new_name})")


# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(
        description="Check saved Trellix KB articles for updates; "
        "report, or --download them as PDF.",
        epilog="Filenames must start with the article id and saved date, e.g. "
        "KB88569_2025.08.07_title.pdf or A000013146_2026.04.16_title.pdf "
        "(date YYYY.MM.DD or YYYYMMDD).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "directory", nargs="?", help="Directory containing saved KB articles"
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Save out-of-date articles as PDF (Playwright); "
        "old files move into OLD/. Without this, only report.",
    )
    parser.add_argument(
        "--add",
        metavar="ID",
        nargs="+",
        help="Download one or more articles by id (e.g. KB88569 A000013146) into the folder.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Ensure a signed-in session (prompts if one exists); combine with --download/report to continue afterwards.",
    )
    parser.add_argument(
        "--session-file",
        default=DEFAULT_SESSION_FILE,
        help=f"Session cookie file (default: {DEFAULT_SESSION_FILE})",
    )
    parser.add_argument(
        "--channel", help="Use an installed browser: 'msedge' or 'chrome'"
    )
    parser.add_argument(
        "--pattern",
        metavar="REGEX",
        help="Extra filename pattern to also recognise, in addition to the "
        "built-in ones. Must be a Python regex with named groups 'id', "
        "'date' and 'name', e.g. "
        r'--pattern "^(?P<id>KB\d+|A\d+)-(?P<date>\d{8})-(?P<name>.+)$"',
    )
    parser.add_argument(
        "--margin",
        help='PDF margin on all four sides, e.g. "1cm", "0.5in" (default 1cm).',
    )
    parser.add_argument(
        "--format", dest="paper", help='PDF paper size, e.g. "A4", "Letter".'
    )
    parser.add_argument(
        "--landscape", action="store_true", help="Print the PDF in landscape."
    )
    parser.add_argument("--scale", type=float, help="PDF render scale (0.1-2.0).")
    parser.add_argument(
        "--lang",
        default=DEFAULT_LANG,
        help=f"Article language (default: {DEFAULT_LANG})",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print verbose debug info (HTTP requests, page resolution, browser "
        "setup) to stderr.",
    )
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug

    if args.pattern:
        try:
            compiled = re.compile(args.pattern)
        except re.error as exc:
            sys.exit(f"Error: --pattern is not a valid regex: {exc}")
        missing = {"id", "date", "name"} - set(compiled.groupindex)
        if missing:
            sys.exit(
                "Error: --pattern is missing named group(s): "
                + ", ".join(sorted(missing))
            )
        FILENAME_PATTERNS.insert(0, compiled)
        debug(f"--pattern: added custom filename pattern {args.pattern!r}")

    if args.login:
        sess = read_session(args.session_file)
        do_login = True
        if sess.exists and sess.valid:
            print(f"A session file was found: {args.session_file}")
            if sess.saved:
                print(f"  saved {sess.saved}  ({sess.detail})")
            do_login = _ask_yes_no("Do you want to log in again?")
            if not do_login:
                print("Keeping the existing session.")
        elif sess.exists and not sess.valid:
            print(
                f"Existing session is no longer usable ({sess.detail}); logging in again."
            )
        if do_login and not interactive_login(args.session_file, args.channel):
            sys.exit(1)
        if not (args.download or args.add):
            return

    if not args.directory:
        sys.exit("Error: a directory is required (or use --login).")
    if not os.path.isdir(args.directory):
        sys.exit(f"Error: {args.directory} is not a valid directory.")

    sess = read_session(args.session_file)
    opener = make_opener(sess.header)

    if args.margin:
        PDF_OPTIONS["margin"] = {
            s: args.margin for s in ("top", "right", "bottom", "left")
        }
    if args.paper:
        PDF_OPTIONS["format"] = args.paper
    if args.landscape:
        PDF_OPTIONS["landscape"] = True
    if args.scale:
        PDF_OPTIONS["scale"] = args.scale

    if args.add:
        for raw_id in args.add:
            run_add(
                args.directory, raw_id, args.lang, args.session_file, args.channel, sess
            )
        return

    try:
        files = [
            f
            for f in sorted(os.listdir(args.directory))
            if os.path.isfile(os.path.join(args.directory, f))
        ]
    except OSError as exc:
        sys.exit(f"Error: could not read {args.directory}: {exc}")
    articles = [p for p in (parse_filename(f) for f in files) if p]

    if not articles:
        print(f"No KB/article files recognised in {args.directory}.")
        return

    if args.download:
        run_download(
            args.directory, articles, opener, args.lang, sess.cookies, args.channel
        )
    else:
        print(build_report(args.directory, articles, opener, args.lang))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as e:
        sys.exit(f"Unexpected error: {e}")

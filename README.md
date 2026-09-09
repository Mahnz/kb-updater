# Trellix KB updater

Checks locally-saved Trellix knowledge base articles against the live portal. By default it prints a report of what is out of date; with `--download` it saves the updated articles as PDF and moves the old files into an `OLD/` subfolder.

## Setup

The report is standard-library only, so `python update.py <directory>` works with no install. `--download`, `--add`, and `--login` drive a real browser via Playwright, so they need one extra step:

```sh
python -m venv .venv
.venv\Scripts\activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
playwright install chromium   # or msedge / chrome, see --channel below
```

## Filenames

Each file starts with its article id and the saved date:

```sh
KB88569_2025.08.07_exclusion-defaults.pdf        legacy KB number
A000013146_2026.04.16_supported-rhel-kernel.pdf  Salesforce article number
```

Date may be `YYYY.MM.DD` or `YYYYMMDD`. Text after the date and the extension are free-form. Files that don't match are ignored.

Filename formats are pluggable: `FILENAME_PATTERNS` in `update.py` is a list of regexes (each exposing `id`, `date`, `name` groups), tried in order. A second built-in pattern already handles e.g. `KB74059 -20250407- this is a kb.pdf`. When a file is updated, the rewritten name keeps the original style (separators and date format) with only the date changed.

For a one-off folder that uses a different convention, pass an extra pattern on the command line instead of editing the file:

```sh
python update.py <dir> --pattern "^(?P<id>KB\d+|A\d+)-(?P<date>\d{8})-(?P<name>.+)$"
```

It must be a Python regex exposing the `id`, `date`, and `name` groups; it is tried before the built-in patterns.

## Usage

```sh
python update.py <directory>              # report only (no browser, no changes)
python update.py <directory> --download   # save updated articles as PDF
python update.py --login                  # sign in once for login-only articles
python update.py <directory> --add KB88569 A000013146  # download one or more articles by id
python update.py <directory> --download --channel msedge
python update.py <directory> --debug                   # verbose diagnostics on stderr
```

`--debug` can be added to any invocation; it prints the HTTP requests, page resolution, and browser setup to stderr without changing normal output.

## How the check works

Trellix has no public KB API, but every article has a plain-HTML printable page:

```sh
https://support.trellix.com/articles/en_US/<Type>/<UrlName>/p
```

`<UrlName>` is the `KB` number for legacy articles or the numeric article number for newer ones (`000013146`, saved locally as `A000013146`). That page carries the "Last Modified Date", so the report needs no browser: it checks both KB- and A-numbered articles over plain HTTP. Login-gated articles return empty until you sign in with `--login`; while unauthenticated they are listed under "LOGIN REQUIRED", and once signed in they resolve like any other article.

## The report

```sh
  OUT OF DATE       article is older than the portal — shown with its URL
  UP TO DATE        matches the portal
  LOGIN REQUIRED    can't be read without signing in (run --login)
```

## `--download` (PDF)

`--download` uses Playwright to open each out-of-date article and save it as a PDF, then moves the superseded file into `OLD/` (nothing is deleted). Install once:

```sh
pip install playwright
playwright install chromium          # or use --channel msedge / chrome
```

## PDF output settings

The PDF is produced by the browser's print-to-PDF, configurable in the `PDF_OPTIONS` block near the top of `update.py`:

```python
PDF_OPTIONS = {
    "format": "A4",            # "A4", "Letter", "Legal", ...
    "landscape": False,
    "scale": 1.0,              # 0.1 - 2.0
    "print_background": True,
    "margin": {"top": "1cm", "right": "1cm", "bottom": "1cm", "left": "1cm"},
}
```

Margins accept CSS units (`1cm`, `0.5in`, `12mm`) and can differ per side. Common settings also have command-line overrides (applied to all four sides for margin):

```sh
python update.py <dir> --download --margin 2cm
python update.py <dir> --download --margin 0.5in --format Letter --landscape --scale 0.9
```

Anything Playwright's `page.pdf()` accepts can be added to `PDF_OPTIONS`. If a value is rejected, the script falls back to a plain print so a run never fails.

## Add an article (`--add`)

Download one or more articles straight into the folder by id, without needing an existing file:

```
python update.py <directory> --add KB88569
python update.py <directory> --add A000013146
python update.py <directory> --add KB88569 A000013146 KB74059
```

Each id fetches the article's last-modified date and title and saves `KB88569_2025.08.07_<title-slug>.pdf`. Uses Playwright (like `--download`). You do not need `--login` first: if the article is login-only, the script says so and asks whether to sign in and retry.

If a file for that article is already in the folder, `--add` says so and reports whether it is up to date. When it is out of date it asks `Update it?` and, on yes, downloads the new version (old file moves to `OLD/`, original naming preserved).

## Login (`--login`)

For login-only articles, run `python update.py --login`. A real browser window opens on the Trellix sign-in page; you sign in (the script never sees your password), and the resulting session cookie is saved to `.trellix_session.json`. Both the report and `--download` reuse it automatically until it expires. Running `--login` again when a valid session exists asks whether to sign in again (and shows when it was saved); if the session has expired it re-signs in directly.

There's no username/password prompt because Trellix uses Okta SSO/MFA, which a scripted login can't handle. Signing in through a real browser window sidesteps that and avoids storing raw credentials.

## Safety

Network, file, and browser operations are guarded individually: a single unreachable article, parse failure, or PDF error is reported and skipped instead of aborting the run. Superseded files are moved, never deleted.

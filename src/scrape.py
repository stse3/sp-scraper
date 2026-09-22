"""Scrape matter metadata and documents from the UARB public documents database.

The site is a FileMaker WebDirect app: everything is rendered by JavaScript and
the matter box is not a real <input>, so we drive it with Playwright and parse
the page's visible text.
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)

URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
DOC_TYPES = ("Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings")
MAX_DOCS = 10

MATTER_RE = re.compile(r"^M\d{5}$")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
EXT_RE = re.compile(r"^\.\w+$")

# The matter box drops keystrokes typed right after focusing, so give it time.
FOCUS_SETTLE_MS = 1000
SEARCH_ATTEMPTS = 3
NOT_FOUND_CONFIRMATIONS = 2  # a dropped keystroke also yields "No Records Found"
ROW_POLLS = 40  # x 500 ms
DOWNLOAD_ATTEMPTS = 3
MAX_FAILED_ROWS = 3  # give up on a tab after this many documents fail outright
MAX_LIST_ROWS = 30  # rows that fit in the tall viewport
PAGE_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 60_000


class ScrapeError(Exception):
    pass


class InvalidMatterNumber(ScrapeError):
    pass


class MatterNotFound(ScrapeError):
    pass


@dataclass
class MatterInfo:
    matter_number: str
    title: str
    type: str
    status: str
    category: str
    date_received: str  # MM/DD/YYYY, "initial filing"
    date_final: str | None  # MM/DD/YYYY, "final filing" (site label: Date Final Submissions)
    counts: dict[str, int]  # doc type -> total documents in the matter


@dataclass
class DocRow:
    doc_no: str
    title: str
    date: str
    security: str
    extension: str
    downloadable: bool  # row has a GO GET IT button

    @property
    def is_public(self) -> bool:
        """Confidential rows still have a GO GET IT button, but it only serves a
        'Confidentiality Notice' placeholder PDF, not the document."""
        return self.security.strip().lower() in ("public", "")


@dataclass
class Downloaded:
    row: DocRow
    paths: list[Path] = field(default_factory=list)
    error: str | None = None


@dataclass
class FetchResult:
    info: MatterInfo
    doc_type: str
    total: int  # documents of this type in the matter
    downloads: list[Downloaded]

    @property
    def files(self) -> list[Path]:
        return [p for d in self.downloads for p in d.paths]


# --------------------------------------------------------------------------
# Pure parsing (unit-tested against saved page text)
# --------------------------------------------------------------------------

def normalize_matter_number(raw: str) -> str:
    matter = raw.strip().upper()
    if not MATTER_RE.match(matter):
        raise InvalidMatterNumber(f"{raw!r} is not a valid matter number (expected e.g. M12205)")
    return matter


def normalize_doc_type(raw: str) -> str:
    wanted = " ".join(raw.split()).lower()
    for doc_type in DOC_TYPES:
        if doc_type.lower() == wanted:
            return doc_type
    raise ValueError(f"{raw!r} is not a document type; expected one of {', '.join(DOC_TYPES)}")


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def parse_counts(text: str) -> dict[str, int]:
    counts = {}
    for doc_type in DOC_TYPES:
        m = re.search(rf"^[ \t]*{doc_type} - (\d+)[ \t]*$", text, re.MULTILINE)
        if not m:
            raise ScrapeError(f"could not find the '{doc_type}' tab count on the matter page")
        counts[doc_type] = int(m.group(1))
    return counts


def parse_matter_text(matter: str, text: str) -> MatterInfo:
    """Parse the matter detail page's visible text.

    After the tab labels the header fields appear in this order:
    matter no, type, status, title, [description], date received, [date final], [outcome], category.
    """
    counts = parse_counts(text)
    lines = _lines(text)
    try:
        i = lines.index(matter)
        matter_type, status, title = lines[i + 1], lines[i + 2], lines[i + 3]
    except (ValueError, IndexError):
        raise ScrapeError(f"could not find {matter}'s header fields on the matter page") from None

    # The header column is 'Title - Description': some matters have a description line
    # under the title, so the dates start at the first date-shaped line, not at a fixed offset.
    j = next((k for k in range(i + 4, len(lines)) if DATE_RE.match(lines[k])), i + 4)
    dates = []
    while j < len(lines) and DATE_RE.match(lines[j]) and len(dates) < 2:
        dates.append(lines[j])
        j += 1
    if not dates or j >= len(lines):
        raise ScrapeError(f"could not find {matter}'s dates/category on the matter page")
    # An Outcome, when the matter has one, sits between the dates and the category.
    end = lines.index("Back to Search Results", j) if "Back to Search Results" in lines[j:] else j + 1
    category = lines[end - 1]

    return MatterInfo(
        matter_number=matter,
        title=title,
        type=matter_type,
        status=status,
        category=category,
        date_received=dates[0],
        date_final=dates[1] if len(dates) > 1 else None,
        counts=counts,
    )


# Page chrome that can sit between the header and the first row of a list.
_LIST_NOISE = {"Date", "Save List", "PDF Only", "Filter by File Extension", "All Types", "Excel/Word/Other"}


def _row_from(head: list[str], tail: list[str]) -> DocRow | None:
    """`head`: the lines before a row's 'Preview' marker. The order differs by tab:
    Other Documents reads id, title, date, security; Exhibits and Key Documents
    read title, date, security, id. The date is the fixed point: security always
    follows it."""
    d = next((k for k, ln in enumerate(head) if DATE_RE.match(ln)), None)
    if d is None:
        return None
    before, after = head[:d], head[d + 2:]
    if after:  # id comes after the security level
        doc_no, title = after[0], " ".join(before)
    else:
        doc_no, title = (before[0] if before else ""), " ".join(before[1:])
    return DocRow(
        doc_no=doc_no,
        title=title,
        date=head[d],
        security=head[d + 1] if d + 1 < len(head) else "",
        extension=next((t for t in tail if EXT_RE.match(t)), ""),
        downloadable="GO GET IT" in tail,
    )


def parse_doc_rows(text: str) -> list[DocRow]:
    """Parse a document list (any of the five document tabs). Every row ends
    with 'Preview', then 'GO GET IT' and '.ext' (the button is absent on rows
    that can't be downloaded)."""
    lines = _lines(text)
    previews = [i for i, ln in enumerate(lines) if ln == "Preview"]
    if not previews:
        return []
    chrome_end = max((i for i in range(previews[0]) if lines[i] == "Tribunal Home"), default=-1)

    rows: list[DocRow] = []
    head: list[str] = []
    i = chrome_end + 1
    while i < len(lines):
        ln = lines[i]
        i += 1
        if ln == "Preview":
            tail: list[str] = []
            while i < len(lines) and len(tail) < 2 and (lines[i] == "GO GET IT" or EXT_RE.match(lines[i])):
                tail.append(lines[i])
                i += 1
            row = _row_from(head, tail)
            if row:
                rows.append(row)
            head = []
        elif ln not in _LIST_NOISE and not ln.startswith("Found Count:"):
            head.append(ln)
    return rows


# --------------------------------------------------------------------------
# Browser automation
# --------------------------------------------------------------------------

def _matter_search_button(page: Page) -> Locator:
    """The Search button on the same row as the matter box. (There are several
    Search buttons; the first in DOM order belongs to the criteria form.)"""
    anchor = page.get_by_text("eg M01234").first.bounding_box()
    if anchor is None:
        raise ScrapeError("matter box is not visible")
    mid = anchor["y"] + anchor["height"] / 2
    buttons = page.get_by_role("button", name="Search", exact=True)
    for i in range(buttons.count()):
        box = buttons.nth(i).bounding_box()
        if box and abs(box["y"] + box["height"] / 2 - mid) < 30 and box["x"] > anchor["x"]:
            return buttons.nth(i)
    raise ScrapeError("could not find the Search button next to the matter box")


def _search_once(page: Page, matter: str) -> str:
    """Type the matter number and press Search. Returns 'found', 'not_found' or
    'unknown' (page never settled / wrong matter came back)."""
    page.goto(URL, wait_until="networkidle", timeout=60_000)
    page.get_by_text("Go Directly to Matter").first.wait_for(timeout=PAGE_TIMEOUT_MS)
    search = _matter_search_button(page)
    # The placeholder sits under the real text field, which intercepts the
    # click; force=True clicks at the placeholder's coordinates, landing on it.
    page.get_by_text("eg M01234").first.click(force=True)
    page.wait_for_timeout(FOCUS_SETTLE_MS)
    page.keyboard.type(matter, delay=40)
    search.click()

    # A hit jumps straight to the matter page (tab labels appear); a miss shows a modal.
    found = page.get_by_text(re.compile(r"^\s*Exhibits - \d+\s*$")).first
    not_found = page.get_by_text("No Records Found").first
    try:
        found.or_(not_found).wait_for(timeout=PAGE_TIMEOUT_MS)
    except PlaywrightTimeout:
        return "unknown"
    return "not_found" if not_found.is_visible() else "found"


def open_matter(page: Page, matter: str) -> MatterInfo:
    not_found = 0
    for attempt in range(1, SEARCH_ATTEMPTS + 1):
        try:
            outcome = _search_once(page, matter)
        except PlaywrightTimeout as e:  # slow page load etc. -- retry from scratch
            log.warning("search %s attempt %d timed out: %s", matter, attempt, str(e).splitlines()[0])
            outcome = "unknown"
        log.info("search %s attempt %d: %s", matter, attempt, outcome)
        if outcome == "found":
            # parse_matter_text raises unless the page is for the matter we asked for
            return parse_matter_text(matter, page.inner_text("body"))
        if outcome == "not_found":
            not_found += 1
            if not_found >= NOT_FOUND_CONFIRMATIONS:
                raise MatterNotFound(f"no matter {matter} in the UARB database")
    raise ScrapeError(f"could not search for {matter} after {SEARCH_ATTEMPTS} attempts")


def _open_tab(page: Page, doc_type: str) -> None:
    page.get_by_text(re.compile(rf"^{doc_type} - \d+$")).first.click()
    page.get_by_text("Preview", exact=True).first.wait_for(timeout=PAGE_TIMEOUT_MS)  # first row rendered


MODAL = ".v-overlay-container"  # WebDirect renders dialogs in this container
MODAL_CHROME = re.compile(r"^(Download Files|Your files are ready for download.*|Close)$", re.DOTALL)


def _modal_filenames(page: Page) -> list[str]:
    """Filenames listed in the 'Download Files' modal: every text leaf inside
    the overlay container except the title, instruction line and Close button."""
    texts = page.evaluate(
        """(sel) => [...document.querySelectorAll(sel + ' *')]
            .filter(e => e.children.length === 0 && e.innerText && e.innerText.trim())
            .map(e => e.innerText.trim())""",
        MODAL,
    )
    return [t for t in dict.fromkeys(texts) if not MODAL_CHROME.match(t)]


def _close_modal(page: Page) -> None:
    close = page.locator(MODAL).get_by_text("Close", exact=True)
    if close.count() and close.first.is_visible():
        close.first.click()
        page.get_by_text("Your files are ready for download").first.wait_for(
            state="hidden", timeout=PAGE_TIMEOUT_MS
        )


def _download_row(page: Page, button: Locator, dest: Path) -> list[Path]:
    button.click()
    page.get_by_text("Your files are ready for download").first.wait_for(timeout=PAGE_TIMEOUT_MS)
    saved = []
    try:
        for name in _modal_filenames(page):
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl:
                page.locator(MODAL).get_by_text(name, exact=True).first.click()
            target = dest / re.sub(r"[^\w.\- ()]", "_", dl.value.suggested_filename or name)
            dl.value.save_as(target)
            saved.append(target)
    finally:
        _close_modal(page)
    if not saved:
        raise ScrapeError("download dialog listed no files")
    return saved


def _wait_for_rows(page: Page, need: int) -> list[DocRow]:
    """The list fills in after the 'Found Count' label appears; poll until
    `need` rows have rendered (or we give up and return what there is)."""
    rows: list[DocRow] = []
    for _ in range(ROW_POLLS):
        rows = parse_doc_rows(page.inner_text("body"))
        if len(rows) >= need:
            break
        page.wait_for_timeout(500)
    return rows


def download_documents(page: Page, count: int, dest: Path, limit: int = MAX_DOCS) -> list[Downloaded]:
    """Download the first `limit` public documents of the tab that is currently
    open, in the site's order. Confidential and undownloadable rows are skipped
    (and reported), not counted towards the limit."""
    dest.mkdir(parents=True, exist_ok=True)
    rows = _wait_for_rows(page, min(count, MAX_LIST_ROWS))
    if len(rows) < min(count, MAX_LIST_ROWS):
        log.warning("only %d of %d rows rendered", len(rows), min(count, MAX_LIST_ROWS))

    buttons = page.get_by_role("button", name="Go Get It")
    button_index = 0  # a button exists on every downloadable row, in row order
    results: list[Downloaded] = []
    delivered = failed = 0
    for row in rows:
        if delivered >= limit or failed >= MAX_FAILED_ROWS:
            break
        result = Downloaded(row)
        results.append(result)
        button = buttons.nth(button_index) if row.downloadable else None
        if row.downloadable:
            button_index += 1
        if not row.downloadable:
            result.error = "no download button"
            continue
        if not row.is_public:
            result.error = f"{row.security}: withheld by the Board"
            continue
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                result.paths = _download_row(page, button, dest)
                result.error = None
                break
            except (PlaywrightTimeout, ScrapeError) as e:
                result.error = f"{type(e).__name__}: {str(e).splitlines()[0]}"
                log.warning("doc %s attempt %d failed: %s", row.doc_no, attempt, result.error)
                try:
                    _close_modal(page)
                except PlaywrightTimeout:
                    pass
        if result.paths:
            delivered += 1
        else:
            failed += 1
    return results


def fetch(matter: str, doc_type: str, dest: Path, limit: int = MAX_DOCS, headless: bool = True) -> FetchResult:
    """Look up `matter`, and download up to `limit` documents of `doc_type` into `dest`."""
    matter = normalize_matter_number(matter)
    doc_type = normalize_doc_type(doc_type)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            # Tall viewport: the document list only renders rows that fit on screen.
            context = browser.new_context(viewport={"width": 1400, "height": 2400}, accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            info = open_matter(page, matter)
            total = info.counts[doc_type]
            downloads = []
            if total:
                _open_tab(page, doc_type)
                downloads = download_documents(page, total, dest, limit)
            return FetchResult(info, doc_type, total, downloads)
        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download UARB matter documents")
    parser.add_argument("matter")
    parser.add_argument("doc_type", help=" | ".join(DOC_TYPES))
    parser.add_argument("--out", type=Path, default=None, help="default: downloads/<matter>")
    parser.add_argument("--limit", type=int, default=MAX_DOCS)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    out = args.out or Path("downloads") / args.matter.upper()
    try:
        result = fetch(args.matter, args.doc_type, out, args.limit, headless=not args.headed)
    except (ScrapeError, ValueError) as e:
        raise SystemExit(f"{type(e).__name__}: {e}")
    print(result.info)
    print(f"{result.doc_type}: {result.total} in matter, downloaded {len(result.files)} file(s) to {out}")
    for d in result.downloads:
        status = ", ".join(p.name for p in d.paths) if d.paths else f"SKIPPED ({d.error})"
        print(f"  {d.row.doc_no}  {d.row.date}  {d.row.title[:60]}  -> {status}")


if __name__ == "__main__":
    main()

"""Compose the reply email body from scraped facts (deterministic, no LLM)."""
from __future__ import annotations

from datetime import datetime

from .package import ZipResult
from .scrape import Downloaded, FetchResult


def _singular(doc_type: str) -> str:
    return doc_type[:-1]  # every doc type is a plural noun ending in "s"


def _join(items: list[str], word: str) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {word} {items[1]}"
    return ", ".join(items[:-1]) + f", {word} {items[-1]}"


def _long_date(s: str) -> str:
    try:
        d = datetime.strptime(s, "%m/%d/%Y")
    except ValueError:
        return s
    return f"{d:%B} {d.day}, {d.year}"


def _greeting(name: str | None) -> str:
    return f"Hi {name}," if name else "Hi there,"


def _counts_sentence(counts: dict[str, int]) -> str:
    found = [f"{n} {t if n != 1 else _singular(t)}" for t, n in counts.items() if n]
    missing = [t for t, n in counts.items() if not n]
    if not found:
        return "I found no documents in this matter."
    if missing:
        found.append("no " + _join(missing, "or"))
    return "I found " + _join(found, "and") + "."


def _download_sentence(doc_type: str, total: int, n: int) -> str:
    if total == 0:
        return f"This matter has no {doc_type}, so the attached ZIP is empty."
    if n == 0:
        return f"I wasn't able to download any of the {total} {doc_type}, so the attached ZIP is empty."
    pronoun = "them" if n > 1 else "it"
    if n == total:
        which = f"all {total}" if total > 1 else "the 1"
        noun = doc_type if total > 1 else _singular(doc_type)
        return f"I downloaded {which} {noun} and am attaching {pronoun} as a ZIP here."
    return f"I downloaded {n} out of the {total} {doc_type} and am attaching {pronoun} as a ZIP here."


def _reason(d: Downloaded, too_large: bool) -> str:
    if too_large:
        return "too large to fit in the email attachment"
    if not d.row.is_public:
        return f"marked {d.row.security} by the Board, so the site only provides a confidentiality notice"
    if not d.row.downloadable:
        return "not available for download on the site"
    return "the download failed after several attempts"


def compose_matter_reply(name: str | None, result: FetchResult, zip_result: ZipResult) -> str:
    info = result.info
    included = set(zip_result.included)
    too_large = set(zip_result.too_large)

    delivered = [d for d in result.downloads if d.paths and all(p in included for p in d.paths)]
    undelivered = [d for d in result.downloads if d not in delivered]

    filing = f"The matter had an initial filing on {_long_date(info.date_received)}"
    if info.date_final:
        filing += f" and a final filing on {_long_date(info.date_final)}"

    paragraph = " ".join(
        [
            f"{info.matter_number} is about {info.title}.",
            f"It relates to {info.type} within the {info.category} category.",
            f"{filing}.",
            _counts_sentence(info.counts),
            _download_sentence(result.doc_type, result.total, len(delivered)),
        ]
    )
    parts = [_greeting(name), paragraph]

    if undelivered:
        lines = ["I couldn't include the following:"]
        for d in undelivered:
            big = any(p in too_large for p in d.paths)
            title = f" ({d.row.title})" if d.row.title else ""
            lines.append(f"- {d.row.doc_no}{title}: {_reason(d, big)}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts) + "\n"


def compose_clarification_reply(name: str | None, reason: str) -> str:
    return (
        f"{_greeting(name)}\n\n{reason}\n\n"
        'For example: "Can you give me Other Documents files from M12205?"\n'
    )


def compose_error_reply(name: str | None, matter: str, doc_type: str) -> str:
    return (
        f"{_greeting(name)}\n\n"
        f"Sorry, I ran into a problem fetching the {doc_type} for {matter} from the UARB website "
        "and couldn't complete your request. Please try again in a few minutes.\n"
    )


def compose_not_found_reply(name: str | None, matter: str) -> str:
    return (
        f"{_greeting(name)}\n\n"
        f"I couldn't find a matter numbered {matter} in the UARB Public Documents database, "
        "so the attached ZIP is empty. Please double-check the matter number "
        "(they look like M12205) and send it again.\n"
    )

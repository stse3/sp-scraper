"""Parser tests, using page text captured from the live site (M12205)."""
import pytest

from src.scrape import (
    InvalidMatterNumber,
    ScrapeError,
    normalize_doc_type,
    normalize_matter_number,
    parse_counts,
    parse_doc_rows,
    parse_matter_text,
)

MATTER_PAGE = """ Exhibits - 13
Key Documents - 6
Other Documents - 43
Transcripts - 0
Recordings - 0
Hearings
Related Matters
M12205

Capital Expenditure Approvals

Open

Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000

04/07/2025

10/23/2025



Water

Back to Search Results
Matter No
Status
"""

DOC_LIST = """Doc No
Security
Title
Found Count: 43
M12205
Capital Expenditure Approvals
Open
Search
More Search Options
Tribunal Home
Date
 
 
Save List
PDF Only
Filter by File Extension
All Types
Excel/Word/Other
102674

Board Order

07/08/2026

Public

Preview
GO GET IT
.pdf

102454

HRWC (Board) Letter - Reply Submission

06/22/2026

Public

Preview
GO GET IT
.pdf

102417

CA (Board) Letter - Comments on Compliance Filing

06/17/2026

Confidential

Preview
.xlsx

102329

Board (HRWC) Compliance Filing - Construction Costs

06/10/2026

Public

Preview
GO GET IT
.pdf
"""


# Exhibits: title, date, security, exhibit no (not numeric). Includes Confidential rows.
EXHIBITS_LIST = """Exhibit No
Security
Title
M12205
Capital Expenditure Approvals
Open
Back to Search Results
Search
More Search Options
Tribunal Home
Save List
PDF Only
Filter by File Extension
All Types
Excel/Word/Other
Found Count: 13
Application
04/07/2025
Public
H-1
Preview
GO GET IT
.pdf
HRWC (Board) RIR-1 to RIR-25 CONFIDENTIAL
05/22/2025
Confidential
H-4(C)
Preview
GO GET IT
.pdf
HRWC (Board) RIR-5 Attachment - HRM Contract Schedule CONFIDENTIAL
05/22/2025
Confidential
H-4(C)-i
Preview
GO GET IT
.pdf
"""

# Key Documents: same field order as Exhibits, but no "Found Count" line and oldest first.
KEY_DOCS_LIST = """Doc No
Security
Title
M12205
Search
More Search Options
Tribunal Home
Save List
Notice of Paper Hearing
04/11/2025
Public
97289
Preview
GO GET IT
.pdf
Board Decision
10/23/2025
Public
99761
Preview
GO GET IT
.pdf
"""


def test_parse_matter_text():
    info = parse_matter_text("M12205", MATTER_PAGE)
    assert info.title == "Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000"
    assert info.type == "Capital Expenditure Approvals"
    assert info.category == "Water"
    assert info.status == "Open"
    assert info.date_received == "04/07/2025"
    assert info.date_final == "10/23/2025"
    assert info.counts == {
        "Exhibits": 13,
        "Key Documents": 6,
        "Other Documents": 43,
        "Transcripts": 0,
        "Recordings": 0,
    }


def test_parse_matter_text_without_final_date():
    page = MATTER_PAGE.replace("10/23/2025\n", "")
    info = parse_matter_text("M12205", page)
    assert info.date_received == "04/07/2025"
    assert info.date_final is None
    assert info.category == "Water"


def test_parse_matter_text_wrong_matter():
    with pytest.raises(ScrapeError):
        parse_matter_text("M99999", MATTER_PAGE)


def test_parse_counts_missing_tab():
    with pytest.raises(ScrapeError):
        parse_counts("Exhibits - 1\nKey Documents - 2")


def test_parse_doc_rows():
    rows = parse_doc_rows(DOC_LIST)
    assert [r.doc_no for r in rows] == ["102674", "102454", "102417", "102329"]
    assert rows[0].title == "Board Order"
    assert rows[0].date == "07/08/2026"
    assert rows[0].extension == ".pdf"
    assert [r.downloadable for r in rows] == [True, True, False, True]
    assert rows[2].security == "Confidential"
    assert rows[2].extension == ".xlsx"


def test_parse_doc_rows_empty():
    assert parse_doc_rows("Found Count: 0") == []


@pytest.mark.parametrize("raw, expected", [("M12205", "M12205"), (" m12383 ", "M12383")])
def test_normalize_matter_number(raw, expected):
    assert normalize_matter_number(raw) == expected


@pytest.mark.parametrize("raw", ["12205", "M1220", "M122055", "MATTER", ""])
def test_normalize_matter_number_invalid(raw):
    with pytest.raises(InvalidMatterNumber):
        normalize_matter_number(raw)


def test_normalize_doc_type():
    assert normalize_doc_type("other documents") == "Other Documents"
    assert normalize_doc_type("Key  Documents") == "Key Documents"
    with pytest.raises(ValueError):
        normalize_doc_type("Hearings")


def test_parse_doc_rows_exhibits_layout():
    rows = parse_doc_rows(EXHIBITS_LIST)
    assert [r.doc_no for r in rows] == ["H-1", "H-4(C)", "H-4(C)-i"]
    assert rows[0].title == "Application" and rows[0].date == "04/07/2025"
    assert [r.security for r in rows] == ["Public", "Confidential", "Confidential"]
    assert all(r.downloadable and r.extension == ".pdf" for r in rows)
    assert rows[2].title == "HRWC (Board) RIR-5 Attachment - HRM Contract Schedule CONFIDENTIAL"


def test_parse_doc_rows_key_documents_layout():
    rows = parse_doc_rows(KEY_DOCS_LIST)
    assert [(r.doc_no, r.title, r.date) for r in rows] == [
        ("97289", "Notice of Paper Hearing", "04/11/2025"),
        ("99761", "Board Decision", "10/23/2025"),
    ]


def test_parse_doc_rows_without_a_list_is_empty():
    assert parse_doc_rows(MATTER_PAGE) == []


# A closed matter: Outcome is filled in, so it sits between the dates and the category.
MATTER_PAGE_WITH_OUTCOME = """ Exhibits - 6
Key Documents - 4
Other Documents - 18
Transcripts - 0
Recordings - 0
Hearings
Related Matters
M12383
Other
Closed
Municipal Boundary - Town of Amherst - 2025 Application for Mutual Boundary Change
07/10/2025
11/28/2025
Allowed/Approved
Municipal Boundaries
Back to Search Results
Matter No
"""


def test_parse_matter_text_with_outcome_does_not_mistake_it_for_the_category():
    info = parse_matter_text("M12383", MATTER_PAGE_WITH_OUTCOME)
    assert info.category == "Municipal Boundaries"
    assert info.type == "Other" and info.status == "Closed"
    assert (info.date_received, info.date_final) == ("07/10/2025", "11/28/2025")
    assert info.counts["Other Documents"] == 18


# Some matters have a description line under the title ("Title - Description" column).
MATTER_PAGE_WITH_DESCRIPTION = """ Exhibits - 4
Key Documents - 3
Other Documents - 4
Transcripts - 0
Recordings - 0
Hearings
Related Matters
M12341
Prior Approval
Closed
Sonnet Insurance Company - S.155G Rate Application 2025 - Private Passenger Vehicles
The overall impact of this proposal is 15.0% uncapped and 14.2% capped with proposed effective dates of September 1, 2025 for new business and October 16, 2025 for renewals.
06/23/2025
08/21/2025
Allowed/Approved
Auto Insurance
Back to Search Results
Matter No
"""


def test_parse_matter_text_with_description_line():
    info = parse_matter_text("M12341", MATTER_PAGE_WITH_DESCRIPTION)
    assert info.title == "Sonnet Insurance Company - S.155G Rate Application 2025 - Private Passenger Vehicles"
    assert info.type == "Prior Approval" and info.category == "Auto Insurance"
    assert (info.date_received, info.date_final) == ("06/23/2025", "08/21/2025")
    assert info.counts["Exhibits"] == 4

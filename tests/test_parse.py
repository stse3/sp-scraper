import pytest

from src.parse import (
    Clarification,
    ExtractedRequest,
    Request,
    clean_email_text,
    parse_request,
    parse_with_regex,
)

EXAMPLE = "Hi Agent, Can you give me Other Documents files from M12205? Thanks!"


def regex(body, subject=""):
    return parse_with_regex(clean_email_text(subject, body))


# ---- regex parser ----

def test_regex_example_from_the_brief():
    assert regex(EXAMPLE) == Request("M12205", "Other Documents", "regex")


@pytest.mark.parametrize(
    "body, doc_type",
    [
        ("exhibits for M12383 please", "Exhibits"),
        ("Need the KEY DOCS from m12383", "Key Documents"),
        ("key-documents M12383", "Key Documents"),
        ("other docs, M 12383", "Other Documents"),
        ("transcript for M12383", "Transcripts"),
        ("Any recordings of M12383?", "Recordings"),
    ],
)
def test_regex_doc_type_variants(body, doc_type):
    result = regex(body)
    assert isinstance(result, Request) and result.doc_type == doc_type and result.matter == "M12383"


def test_regex_matter_in_subject():
    assert regex("Can I get the transcripts?", subject="Documents for M12205") == Request(
        "M12205", "Transcripts", "regex"
    )


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("Can you send me the exhibits?", "matter number"),
        ("Please send everything for M12205", "type of document"),
        ("Exhibits and transcripts for M12205", "several document types"),
        ("Exhibits for M12205 and M12383", "several matters"),
        ("Exhibits for matter 12205", "matter number"),  # must be M + 5 digits
    ],
)
def test_regex_asks_for_clarification(body, fragment):
    result = regex(body)
    assert isinstance(result, Clarification) and fragment in result.reason


def test_quoted_history_is_ignored():
    body = (
        "Actually, the transcripts please.\n\n"
        "On Mon, Sep 14, 2026 at 9:00 AM Agent <agent@example.com> wrote:\n"
        "> M12205 exhibits attached\n"
    )
    assert regex(body, subject="Re: M12205") == Request("M12205", "Transcripts", "regex")


def test_quoted_matter_number_alone_is_not_a_request():
    body = "Thanks!\n\n> Exhibits for M12205"
    assert isinstance(regex(body), Clarification)


# ---- LLM path (fake extractor) ----

def fake(**kw):
    return lambda text: ExtractedRequest(**kw)


def test_llm_result_is_used_when_valid():
    result = parse_request("", EXAMPLE, extract=fake(matter_number="M12205", doc_type="other documents"))
    assert result == Request("M12205", "Other Documents", "llm")


def test_llm_can_handle_wording_regex_cannot():
    body = "Could you grab the paperwork the utility filed as evidence on M12205?"
    result = parse_request("", body, extract=fake(matter_number="M12205", doc_type="Exhibits"))
    assert result == Request("M12205", "Exhibits", "llm")


def test_llm_clarification_is_passed_through():
    result = parse_request("", "hello", extract=fake(clarification="Which matter do you mean?"))
    assert result == Clarification("Which matter do you mean?", "llm")


def test_llm_invented_matter_is_rejected_then_regex_used():
    result = parse_request("", EXAMPLE, extract=fake(matter_number="M99999", doc_type="Exhibits"))
    assert result == Request("M12205", "Other Documents", "regex")


@pytest.mark.parametrize(
    "kw",
    [
        {"matter_number": "M12205", "doc_type": "Hearings"},  # not a supported type
        {"matter_number": "M1220", "doc_type": "Exhibits"},  # malformed number
        {"matter_number": "M12205"},  # missing type
        {},  # nothing at all
    ],
)
def test_llm_invalid_output_falls_back_to_regex(kw):
    result = parse_request("", EXAMPLE, extract=fake(**kw))
    assert result == Request("M12205", "Other Documents", "regex")


def test_llm_error_falls_back_to_regex():
    def boom(text):
        raise TimeoutError("api down")

    assert parse_request("", EXAMPLE, extract=boom) == Request("M12205", "Other Documents", "regex")


def test_no_api_key_uses_regex(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert parse_request("", EXAMPLE) == Request("M12205", "Other Documents", "regex")


def test_llm_sees_only_the_new_message():
    seen = []

    def spy(text):
        seen.append(text)
        return ExtractedRequest(matter_number="M12205", doc_type="Transcripts")

    body = "Transcripts for M12205\n\n> M99999 old stuff"
    parse_request("Re: docs", body, extract=spy)
    assert "M99999" not in seen[0] and "Re: docs" in seen[0]


# ---- Gemini SDK wiring ----

def test_response_schema_is_accepted_by_the_sdk():
    from google import genai
    from google.genai import _transformers as transformers

    client = genai.Client(api_key="not-a-real-key")
    schema = transformers.t_schema(client._api_client, ExtractedRequest)
    assert set(schema.properties) == {"matter_number", "doc_type", "clarification"}

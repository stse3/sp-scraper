import os
import zipfile
from collections import Counter
from email.message import EmailMessage

import pytest

from src import agent
from src.mail import IncomingEmail, MailConfig, build_reply, first_name, parse_message
from src.scrape import DocRow, Downloaded, FetchResult, MatterInfo, MatterNotFound

CFG = MailConfig("agent@gmail.com", "pw")


def raw_email(frm="Sherry Tse <sherry@example.com>", subject="Docs please", body="Exhibits for M12205", **headers):
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = "agent@gmail.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<abc@example.com>"
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    msg.set_content(body)
    return msg


def parsed(msg, allow_self=False):
    return parse_message("7", msg.as_bytes(), CFG.address, allow_self)


# ---- parsing incoming mail ----

def test_parse_plain_email():
    mail = parsed(raw_email())
    assert (mail.sender_name, mail.sender_addr) == ("Sherry Tse", "sherry@example.com")
    assert mail.subject == "Docs please" and "M12205" in mail.body
    assert mail.message_id == "<abc@example.com>" and mail.skip_reason is None


def test_parse_html_only_email():
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "a@example.com", "Hi"
    msg.set_content("<p>Transcripts for <b>M12205</b> &amp; thanks</p>", subtype="html")
    body = " ".join(parsed(msg).body.split())
    assert "<" not in body and "&amp;" not in body
    assert body.startswith("Transcripts for") and "M12205" in body and "& thanks" in body


def test_parse_ignores_attachments_in_body():
    msg = raw_email(body="Recordings for M12205")
    msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="x.pdf")
    assert parsed(msg).body.strip() == "Recordings for M12205"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frm": "Google <no-reply@accounts.google.com>"},
        {"frm": "Mail Delivery <MAILER-DAEMON@googlemail.com>"},
        {"frm": "bot@example.com", "Auto_Submitted": "auto-replied"},
        {"frm": "news@example.com", "Precedence": "bulk"},
        {"frm": "list@example.com", "List_Id": "<list.example.com>"},
        {"frm": "agent@gmail.com"},
    ],
)
def test_automated_mail_is_skipped(kwargs):
    assert parsed(raw_email(**kwargs)).skip_reason is not None


def test_self_mail_allowed_only_when_testing():
    assert parsed(raw_email(frm="agent@gmail.com"), allow_self=True).skip_reason is None


@pytest.mark.parametrize(
    "display, expected",
    [("Sherry Tse", "Sherry"), ('"Tse, Sherry"', "Sherry"), ("", None), ("sherry@example.com", None), ("Madonna", "Madonna")],
)
def test_first_name(display, expected):
    assert first_name(display) == expected


# ---- building the reply ----

def incoming(**kw):
    base = dict(uid="7", sender_name="Sherry Tse", sender_addr="sherry@example.com", subject="Docs please",
                body="Exhibits for M12205", message_id="<abc@example.com>", references="<old@example.com>")
    return IncomingEmail(**{**base, **kw})


def test_reply_is_threaded_and_marked_automatic(tmp_path):
    z = tmp_path / "M12205_Exhibits.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.pdf", "x")
    msg = build_reply(CFG, incoming(), "Hello", attachment=z)
    assert msg["To"] == "sherry@example.com" and msg["From"] == "agent@gmail.com"
    assert msg["Subject"] == "Re: Docs please"
    assert msg["In-Reply-To"] == "<abc@example.com>"
    assert msg["References"] == "<old@example.com> <abc@example.com>"
    assert msg["Auto-Submitted"] == "auto-replied"
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "M12205_Exhibits.zip"
    assert attachment.get_content_type() == "application/zip"


def test_reply_subject_not_doubled_or_empty():
    assert build_reply(CFG, incoming(subject="Re: Docs"), "x")["Subject"] == "Re: Docs"
    assert build_reply(CFG, incoming(subject=""), "x")["Subject"] == "Re: your request"


# ---- agent.handle ----

@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(agent, "send", lambda cfg, msg: messages.append(msg))
    return messages


def fake_result(tmp_path_factory=None):
    info = MatterInfo("M12205", "Some Project", "Capital Expenditure Approvals", "Open", "Water",
                      "04/07/2025", "10/23/2025",
                      {"Exhibits": 1, "Key Documents": 0, "Other Documents": 0, "Transcripts": 0, "Recordings": 0})
    return info


def test_clarification_gets_no_attachment(sent):
    agent.handle(CFG, incoming(body="hello there", subject="hi"))
    (msg,) = sent
    assert list(msg.iter_attachments()) == []
    assert "matter number" in msg.get_body().get_content()


def test_not_found_gets_empty_zip(sent, monkeypatch):
    def not_found(*a, **k):
        raise MatterNotFound("nope")

    monkeypatch.setattr(agent, "fetch", not_found)
    agent.handle(CFG, incoming(body="Exhibits for M99999"))
    (msg,) = sent
    assert "couldn't find a matter numbered M99999" in msg.get_body().get_content()
    (att,) = list(msg.iter_attachments())
    import io
    assert zipfile.ZipFile(io.BytesIO(att.get_content())).namelist() == []


def test_success_attaches_zip_of_downloaded_files(sent, monkeypatch):
    def fake_fetch(matter, doc_type, dest, **kw):
        dest.mkdir(parents=True)
        f = dest / "100.pdf"
        f.write_bytes(b"pdf-bytes")
        row = DocRow("100", "A filing", "01/01/2026", "Public", ".pdf", True)
        return FetchResult(fake_result(), doc_type, 1, [Downloaded(row, [f])])

    monkeypatch.setattr(agent, "fetch", fake_fetch)
    agent.handle(CFG, incoming(body="Exhibits for M12205"))
    (msg,) = sent
    body = msg.get_body().get_content()
    assert "M12205 is about Some Project." in body and body.startswith("Hi Sherry,")
    import io
    (att,) = list(msg.iter_attachments())
    assert zipfile.ZipFile(io.BytesIO(att.get_content())).read("100.pdf") == b"pdf-bytes"


def test_scrape_failure_sends_apology_without_attachment(sent, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("site down")

    monkeypatch.setattr(agent, "fetch", broken)
    agent.handle(CFG, incoming(body="Exhibits for M12205"))
    (msg,) = sent
    assert "ran into a problem" in msg.get_body().get_content()
    assert list(msg.iter_attachments()) == []


# ---- agent.process_inbox ----

def test_process_inbox_marks_skipped_and_answered_mail_seen(monkeypatch):
    mails = [incoming(uid="1", skip_reason="automated sender"), incoming(uid="2")]
    seen, handled = [], []
    monkeypatch.setattr(agent, "fetch_unseen", lambda cfg, allow_self=False: mails)
    monkeypatch.setattr(agent, "mark_seen", lambda cfg, uid: seen.append(uid))
    monkeypatch.setattr(agent, "handle", lambda cfg, mail: handled.append(mail.uid))
    assert agent.process_inbox(CFG, Counter()) == 1
    assert handled == ["2"] and seen == ["1", "2"]


def test_failing_email_is_retried_then_given_up_on(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "fetch_unseen", lambda cfg, allow_self=False: [incoming(uid="9")])
    monkeypatch.setattr(agent, "mark_seen", lambda cfg, uid: seen.append(uid))

    def smtp_down(cfg, mail):
        raise OSError("smtp down")

    monkeypatch.setattr(agent, "handle", smtp_down)
    failures = Counter()
    for _ in range(agent.MAX_ATTEMPTS - 1):
        assert agent.process_inbox(CFG, failures) == 0
    assert seen == []  # still unread: will be retried
    agent.process_inbox(CFG, failures)
    assert seen == ["9"]  # gave up, so it can't block the loop forever


# ---- single instance ----

def test_second_instance_is_refused_until_the_first_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "LOCK_PATH", tmp_path / "agent.lock")
    first = agent.acquire_single_instance_lock()
    with pytest.raises(SystemExit) as exc:
        agent.acquire_single_instance_lock()
    assert "already running" in str(exc.value)
    assert str(os.getpid()) in str(exc.value)  # names the process to stop
    first.close()  # what the OS does when the process exits
    agent.acquire_single_instance_lock().close()

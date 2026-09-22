"""The agent loop: poll the inbox, fulfil each request, reply."""
from __future__ import annotations

import argparse
import logging
import os
import tempfile
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

try:
    import fcntl
except ImportError:  # Windows: no lock, run a single instance yourself
    fcntl = None

from .mail import IncomingEmail, MailConfig, build_reply, fetch_unseen, first_name, mark_seen, send
from .package import build_zip
from .parse import Clarification, parse_request
from .reply import (
    compose_clarification_reply,
    compose_error_reply,
    compose_matter_reply,
    compose_not_found_reply,
)
from .scrape import MatterNotFound, fetch

log = logging.getLogger(__name__)

LOCK_PATH = Path(__file__).resolve().parent.parent / ".agent.lock"
MAX_ATTEMPTS = 3  # an email that keeps failing is marked read so it can't wedge the loop


def handle(cfg: MailConfig, mail: IncomingEmail) -> None:
    """Answer one email. Raises only if the reply itself could not be sent."""
    name = first_name(mail.sender_name)
    parsed = parse_request(mail.subject, mail.body)
    log.info("email from %s -> %s", mail.sender_addr, parsed)

    if isinstance(parsed, Clarification):
        send(cfg, build_reply(cfg, mail, compose_clarification_reply(name, parsed.reason)))
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        zip_path = tmp / f"{parsed.matter}_{parsed.doc_type.replace(' ', '_')}.zip"
        try:
            result = fetch(parsed.matter, parsed.doc_type, tmp / "docs")
        except MatterNotFound:
            zipped = build_zip([], zip_path)
            body = compose_not_found_reply(name, parsed.matter)
        except Exception:
            log.exception("scrape failed for %s %s", parsed.matter, parsed.doc_type)
            send(cfg, build_reply(cfg, mail, compose_error_reply(name, parsed.matter, parsed.doc_type)))
            return
        else:
            zipped = build_zip(result.files, zip_path)
            body = compose_matter_reply(name, result, zipped)
        send(cfg, build_reply(cfg, mail, body, attachment=zipped.path))
        log.info("replied to %s (%d file(s) zipped)", mail.sender_addr, len(zipped.included))


def process_inbox(cfg: MailConfig, failures: Counter, allow_self: bool = False) -> int:
    """One poll. Returns the number of emails answered."""
    answered = 0
    for mail in fetch_unseen(cfg, allow_self):
        if mail.skip_reason:
            log.info("skipping email from %s (%s)", mail.sender_addr, mail.skip_reason)
            mark_seen(cfg, mail.uid)
            continue
        try:
            handle(cfg, mail)
        except Exception:
            failures[mail.uid] += 1
            log.exception("could not answer email %s (attempt %d/%d)", mail.uid, failures[mail.uid], MAX_ATTEMPTS)
            if failures[mail.uid] >= MAX_ATTEMPTS:
                mark_seen(cfg, mail.uid)
            continue
        mark_seen(cfg, mail.uid)
        answered += 1
    return answered


def acquire_single_instance_lock():
    """Refuse to start if another agent is already running. Two agents polling
    one inbox both pick up the same unread email and each send a reply. The
    lock is released by the OS when the process exits, however it exits."""
    if fcntl is None:
        return None
    lock = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        holder = lock.read().strip() or "unknown"
        lock.close()
        raise SystemExit(
            f"Another agent is already running (pid {holder}). Stop it first "
            f"(Ctrl+C in its terminal, or `kill {holder}`), otherwise both would answer the same email."
        ) from None
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock  # keep a reference: closing the file releases the lock


def main() -> None:
    parser = argparse.ArgumentParser(description="UARB regulatory document agent")
    parser.add_argument("--once", action="store_true", help="process waiting emails and exit")
    parser.add_argument("--interval", type=int, default=30, help="seconds between inbox checks")
    parser.add_argument("--allow-self", action="store_true", help="answer mail sent from the agent's own address (testing)")
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    lock = acquire_single_instance_lock()  # held until the process exits
    cfg = MailConfig.from_env()

    failures: Counter = Counter()
    log.info("watching %s (every %ds)", cfg.address, args.interval)
    while True:
        try:
            process_inbox(cfg, failures, args.allow_self)
        except Exception:  # IMAP hiccup etc.: keep the agent alive
            log.exception("inbox check failed")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

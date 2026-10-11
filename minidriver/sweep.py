"""The upload sweep.

When the box reaches home it sends every capture that has not gone to each
service yet -- not just the current session's, because a drive done out of any
home network is uploaded whenever the box next has one, and the operator should
not have to remember which captures are still outstanding.

Adapted from m1ndb0x's shared survey-upload machinery, with the payload and UI
coupling removed: the clients are passed in, so this is ordinary testable code.
A capture is skipped (not re-sent) when its exact content has already gone, and
one that has grown since is offered again because what went up was only part.
"""
import logging
from pathlib import Path

from minidriver.survey import records_in, strip_unlocated
from minidriver.uploads import UploadError, sha256_of

log = logging.getLogger(__name__)


def pending_for(captures, service: str, ledger) -> list:
    """Captures not yet sent to *service* at their current size.

    Filtered before the work of preparing them: a capture already sent at this
    size buys nothing by being re-read. One that has grown has a new size and so
    is offered again.
    """
    waiting = []
    for capture in captures or []:
        capture = Path(capture)
        try:
            size = capture.stat().st_size
        except OSError:
            continue
        if size == 0 or ledger.sent_source(service, capture, size):
            continue
        waiting.append(capture)
    return waiting


def prepare_capture(capture, service: str) -> Path:
    """A CSV ready for *service*: the capture with positionless rows stripped.

    The survey is already WigleWifi-1.6, which all three services take, so there
    is no conversion -- only the filtering both sides would do anyway, done once
    here so the ledger records exactly what shipped. Written to a service-named
    copy because the live capture may still be growing.
    """
    capture = Path(capture)
    kept = strip_unlocated(capture.read_text(errors="replace"))
    if not records_in(kept):
        raise UploadError("nothing located in this capture")
    out = capture.with_name(f"{capture.stem}.{service}.csv")
    out.write_text(kept, encoding="utf-8")
    return out


def upload_pending(service: str, captures, ledger, client,
                   prepare=prepare_capture) -> dict:
    """Send every pending capture to *service* through *client*.

    Returns {"sent", "skipped", "errors"}. A capture with nothing located, or
    whose content was already sent, is recorded and skipped rather than retried
    on every sweep; a real upload failure is collected and the sweep goes on to
    the next capture, so one bad file does not strand the rest.
    """
    sent = skipped = 0
    errors = []
    for capture in pending_for(captures, service, ledger):
        size = capture.stat().st_size
        try:
            csv_path = prepare(capture, service)
        except UploadError as exc:
            if "nothing located" in str(exc).lower():
                # Record a synthetic digest so the empty capture is not examined
                # again on every sweep; nothing was actually sent.
                ledger.record(service, f"empty:{size}", capture.name,
                              {"skipped": "nothing located"}, source=capture)
                skipped += 1
                continue
            errors.append(str(exc))
            continue

        digest = sha256_of(csv_path)
        if ledger.already_sent(service, digest):
            ledger.record(service, digest, csv_path.name,
                          {"skipped": "identical content"}, source=capture)
            skipped += 1
            continue

        try:
            result = client.upload(csv_path)
        except UploadError as exc:
            errors.append(str(exc))
            continue
        ledger.record(service, digest, csv_path.name, result, source=capture)
        sent += 1
        log.info("uploaded %s to %s", csv_path.name, service)

    return {"sent": sent, "skipped": skipped, "errors": errors}

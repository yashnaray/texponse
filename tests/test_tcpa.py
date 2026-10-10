from __future__ import annotations

from texponse.db.models import SmsConsentStatus
from texponse.db.repositories import SmsSuppressionRepository
from texponse.security.tcpa import (
    SmsKeyword,
    classify_sms_keyword,
    help_reply,
    opt_in_confirmation,
    opt_out_confirmation,
)


def test_classify_opt_out_variants():
    for text in ("STOP", "stop", "Stop all", "STOPALL", "unsubscribe", "CANCEL", "END", "quit"):
        assert classify_sms_keyword(text) == SmsKeyword.OPT_OUT, text


def test_classify_opt_in_and_help():
    for text in ("START", "yes", "UNSTOP"):
        assert classify_sms_keyword(text) == SmsKeyword.OPT_IN, text
    for text in ("HELP", "help please", "INFO"):
        assert classify_sms_keyword(text) == SmsKeyword.HELP, text


def test_classify_none():
    assert classify_sms_keyword(None) == SmsKeyword.NONE
    assert classify_sms_keyword("") == SmsKeyword.NONE
    assert classify_sms_keyword("When is my appointment?") == SmsKeyword.NONE
    # Keyword must be the FIRST token, not buried in prose.
    assert classify_sms_keyword("Please stop calling me") == SmsKeyword.NONE


def test_canned_replies_identify_sender_and_offer_recourse():
    assert "Acme" in opt_out_confirmation("Acme")
    assert "START" in opt_out_confirmation("Acme")
    assert "STOP" in opt_in_confirmation("Acme")
    assert "STOP" in help_reply("Acme")


async def test_suppression_opt_out_wins(async_session):
    repo = SmsSuppressionRepository(async_session)
    assert await repo.is_suppressed("c1", ["h1"]) is False
    assert await repo.is_suppressed("c1", []) is False

    await repo.set_status("c1", ["h1"], SmsConsentStatus.OPTED_OUT, keyword="stop")
    assert await repo.is_suppressed("c1", ["h1"]) is True
    # Other companies unaffected.
    assert await repo.is_suppressed("c2", ["h1"]) is False

    # Re-subscribe lifts the block.
    await repo.set_status("c1", ["h1"], SmsConsentStatus.OPTED_IN, keyword="start")
    assert await repo.is_suppressed("c1", ["h1"]) is False


async def test_suppression_matches_any_pepper_hash(async_session):
    repo = SmsSuppressionRepository(async_session)
    await repo.set_status("c1", ["h1", "h2"], SmsConsentStatus.OPTED_OUT, keyword="stop")
    assert await repo.is_suppressed("c1", ["h2"]) is True
    assert await repo.is_suppressed("c1", ["other"]) is False

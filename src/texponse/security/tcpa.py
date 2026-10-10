"""TCPA/CTIA SMS compliance helpers.

Covers what the app controls for its conversational (solicited-reply) SMS:

- Opt-out keywords (STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT) per the
  CTIA keyword list — inbound match suppresses ALL future outbound SMS to
  that caller except the one-time confirmation (which carriers require).
- Opt-in keywords (START, YES, UNSTOP) lift suppression.
- HELP/INFO returns a help reply identifying the sender (CTIA requires
  HELP handling and sender identification on every program).
- A persistent per-company suppression list (see SmsSuppression) checked
  before every send.

Out of scope here (operational, not code): 10DLC brand/campaign
registration, prior-express-consent capture at lead intake, and quiet
hours — this service only replies to consumer-initiated messages, which
are exempt from the FCC's 8am–9pm marketing quiet-hour rule. If proactive
(marketing) sends are added later, they must check consent + quiet hours
before queueing.
"""

from __future__ import annotations

from enum import StrEnum


class SmsKeyword(StrEnum):
    OPT_OUT = "opt_out"
    OPT_IN = "opt_in"
    HELP = "help"
    NONE = "none"


OPT_OUT_KEYWORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit"})

OPT_IN_KEYWORDS = frozenset({"start", "yes", "unstop"})

HELP_KEYWORDS = frozenset({"help", "info"})


def classify_sms_keyword(content: str | None) -> SmsKeyword:
    """Map inbound message text to a TCPA keyword action.

    Matches the CTIA single-keyword convention: the first whitespace-delimited
    token, case-insensitive (e.g. "STOP", "Stop all", "HELP please").
    Anything else is NONE (normal conversational flow).
    """
    if not content:
        return SmsKeyword.NONE
    first = content.strip().lower().split()[0] if content.strip() else ""
    if first in OPT_OUT_KEYWORDS:
        return SmsKeyword.OPT_OUT
    if first in OPT_IN_KEYWORDS:
        return SmsKeyword.OPT_IN
    if first in HELP_KEYWORDS:
        return SmsKeyword.HELP
    return SmsKeyword.NONE


def opt_out_confirmation(sender_name: str) -> str:
    """One-time confirmation carriers require after an opt-out (always allowed)."""
    return (
        f"{sender_name}: You have been unsubscribed and will receive no further "
        "texts. Reply START to re-subscribe."
    )


def opt_in_confirmation(sender_name: str) -> str:
    return (
        f"{sender_name}: You are re-subscribed and will receive replies to your "
        "messages. Reply STOP to opt out, HELP for help."
    )


def help_reply(sender_name: str) -> str:
    """CTIA-required HELP response: identity + what the program does + opt-out."""
    return (
        f"{sender_name}: We reply to your service messages. Msg&data rates may "
        "apply. Reply STOP to opt out."
    )

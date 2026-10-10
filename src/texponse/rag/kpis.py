"""KPI definitions + computation from Jobber GraphQL data.

All 23 KPIs from the spec. Each formula is documented with its numerator /
denominator so the endpoint response is auditable. Data is fetched live via
JobberClient.graphql; pure math lives in ``compute_kpis`` so it is unit
testable without API access.
"""

from __future__ import annotations

import re as _re
from calendar import monthrange as _monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class KPIDefinition:
    key: str
    name: str
    category: str
    formula: str
    description: str
    important: bool = False


KPI_DEFINITIONS: list[KPIDefinition] = [
    # 1. Sales & Conversion
    KPIDefinition(
        "lead_booking_rate",
        "Lead Booking Rate",
        "sales_conversion",
        "(Contacts with a Job Created within 14 Days / Total Inbound Contacts) x 100",
        "Share of inbound SMS contacts converting to a job within 14 days "
        "(voice calls not tracked; staff lines excluded).",
        True,
    ),
    KPIDefinition(
        "in_home_opportunity_conversion_rate",
        "In-Home Opportunity Conversion Rate",
        "sales_conversion",
        "(Jobs Invoiced Above Trip/Diagnostic Charge / Total In-Home Opportunities) x 100",
        "Share of on-site visits producing paid repair/replacement revenue.",
        True,
    ),
    KPIDefinition(
        "quote_close_rate",
        "Quote Close Rate (Win Rate)",
        "sales_conversion",
        "(Won Quotes / Total Drafted Quotes) x 100",
        "Share of formal estimates approved.",
        True,
    ),
    KPIDefinition(
        "sales_to_produced_revenue_ratio",
        "Sales-to-Produced Revenue Ratio",
        "sales_conversion",
        "Total Sold Quote Value / Total Invoiced Revenue (Completed)",
        "Pipeline replenishment velocity.",
        False,
    ),
    # 2. Operational & Technician
    KPIDefinition(
        "average_ticket",
        "Average Ticket / Revenue per Completed Job",
        "operations",
        "Total Invoiced Job Revenue / Total Completed Jobs Count",
        "Average dollars per completed job.",
        True,
    ),
    KPIDefinition(
        "callback_rate",
        "Callback Rate",
        "operations",
        "(Jobs Requiring Unpaid Return within 30 Days / Total Completed Jobs) x 100",
        "Lower = higher first-time fix rate.",
        True,
    ),
    KPIDefinition(
        "technician_average_ticket",
        "Technician Average Ticket",
        "operations",
        "Total Revenue Credited to Technician / Technician True Completed Jobs Count",
        "Per-tech ticket size and upsell effectiveness.",
        False,
    ),
    KPIDefinition(
        "technician_labor_cost_pct",
        "Technician Labor Cost Percentage",
        "operations",
        "(Technician Direct Pay / Invoiced Revenue Credited) x 100",
        "Field direct labor burden vs produced revenue.",
        False,
    ),
    KPIDefinition(
        "on_time_arrival_rate",
        "On-Time Arrival Rate",
        "operations",
        "(On-Time Dispatched Arrivals / Total Completed Appointments) x 100",
        "Punctuality vs appointment windows.",
        False,
    ),
    # 3. Financial & Job Costing
    KPIDefinition(
        "recorded_cost_margin_pct",
        "Recorded-Cost Margin % (Materials Margin)",
        "financial",
        "((Invoiced Revenue - Material & Equipment Costs) / Invoiced Revenue) x 100",
        "Gross margin before labor/commissions/overhead.",
        True,
    ),
    KPIDefinition(
        "contribution_profit",
        "Contribution Profit ($)",
        "financial",
        "Invoiced Revenue - (Materials + Tech Pay + Commissions + Processing Fees)",
        "Cash dollars left for overhead and profit.",
        True,
    ),
    KPIDefinition(
        "contribution_margin_pct",
        "Contribution Margin (%)",
        "financial",
        "(Contribution Profit / Invoiced Revenue) x 100",
        "Revenue retained after direct variable costs.",
        False,
    ),
    KPIDefinition(
        "net_contribution_after_office_pct",
        "Net Contribution After Office Staff (%)",
        "financial",
        "((Contribution Profit - Office Salaries) / Invoiced Revenue) x 100",
        "Margin after field + office support pay.",
        False,
    ),
    KPIDefinition(
        "ar_aging_bucket_share",
        "AR Aging Bucket Share",
        "financial",
        "(Unpaid AR Balance in Category / Total Outstanding AR) x 100",
        "Collection efficiency / bad-debt exposure.",
        False,
    ),
    # 4. Recurring & Reputation
    KPIDefinition(
        "maintenance_offer_rate",
        "Maintenance Plan (Pros Club) Offer Rate",
        "recurring",
        "(Jobs with Documented Plan Offer / Total Eligible Residential Jobs) x 100",
        "How often plans are presented.",
        False,
    ),
    KPIDefinition(
        "maintenance_close_rate",
        "Maintenance Plan Attachment / Close Rate",
        "recurring",
        "(Plans Sold / Plans Offered) x 100",
        "Acceptance rate when offered.",
        False,
    ),
    KPIDefinition(
        "google_review_rate",
        "Google Review Acquisition Rate",
        "recurring",
        "(Reviews in Period / Total Eligible Completed Visits) x 100",
        "Satisfaction / review capture rate.",
        False,
    ),
    # 5. Call Center & Marketing
    KPIDefinition(
        "missed_call_rate",
        "Missed Call / Abandonment Rate",
        "marketing",
        "(Unanswered Inbound Calls / Total Inbound Calls) x 100",
        "Front-office capture responsiveness.",
        True,
    ),
    KPIDefinition(
        "lead_to_job_by_channel",
        "Lead-to-Job Conversion Rate by Channel",
        "marketing",
        "(Completed Jobs per Source / Inbound Leads per Source) x 100",
        "Traffic quality per ad platform.",
        False,
    ),
    KPIDefinition(
        "cost_per_qualified_lead",
        "Cost Per Qualified Lead (CPL)",
        "marketing",
        "Channel Spend / Qualified Inbound Leads",
        "Spend efficiency by channel.",
        False,
    ),
    # Derived extras to reach full operational coverage
    KPIDefinition(
        "total_invoiced_revenue",
        "Total Invoiced Revenue",
        "financial",
        "SUM(job.total where invoiced)",
        "Base produced revenue.",
        False,
    ),
    KPIDefinition(
        "total_completed_jobs",
        "Total Completed Jobs",
        "operations",
        "COUNT(jobs with completed status)",
        "Base volume.",
        False,
    ),
    KPIDefinition(
        "total_quotes",
        "Total Quotes Drafted",
        "sales_conversion",
        "COUNT(quotes drafted/presented)",
        "Base pipeline volume.",
        False,
    ),
]


KPI_KEYWORDS: dict[str, list[str]] = {
    "lead_booking_rate": ["booking rate", "lead booking", "book rate", "conversion rate"],
    "in_home_opportunity_conversion_rate": [
        "in-home conversion",
        "in home conversion",
        "close rate on site",
    ],
    "quote_close_rate": [
        "quote close",
        "win rate",
        "quote approval",
        "estimate approval",
        "close rate",
    ],
    "sales_to_produced_revenue_ratio": [
        "sales to produced",
        "pipeline velocity",
        "sold vs invoiced",
    ],
    "average_ticket": ["average ticket", "avg ticket", "revenue per job", "average job value"],
    "callback_rate": [
        "callback rate",
        "callbacks",
        "warranty return",
        "first-time fix",
        "first time fix",
    ],
    "technician_average_ticket": ["technician average", "tech ticket", "per tech revenue"],
    "technician_labor_cost_pct": ["labor cost", "labor burden", "tech pay percent"],
    "on_time_arrival_rate": ["on-time", "on time arrival", "punctuality", "late arrival"],
    "recorded_cost_margin_pct": ["margin", "materials margin", "gross margin", "material cost"],
    "contribution_profit": ["contribution profit", "contribution dollars", "profit dollars"],
    "contribution_margin_pct": ["contribution margin"],
    "net_contribution_after_office_pct": [
        "net contribution",
        "after office",
        "office staff margin",
    ],
    "ar_aging_bucket_share": [
        "ar aging",
        "receivables",
        "a/r",
        "unpaid balance",
        "collections",
        "bad debt",
    ],
    "maintenance_offer_rate": ["plan offer rate", "pros club offer", "maintenance offer"],
    "maintenance_close_rate": ["plan attachment", "plan close rate", "maintenance attachment"],
    "google_review_rate": ["review rate", "google reviews", "review capture"],
    "missed_call_rate": ["missed call", "abandonment", "abandoned call", "call capture"],
    "lead_to_job_by_channel": ["by channel", "per channel", "lead source", "marketing channel"],
    "cost_per_qualified_lead": ["cost per lead", "cpl", "cost per qualified"],
    "total_invoiced_revenue": ["total revenue", "invoiced revenue", "how much revenue"],
    "total_completed_jobs": ["how many jobs", "completed jobs", "job count"],
    "total_quotes": ["how many quotes", "quote count", "quotes drafted"],
}


def detect_kpi_intent(query: str) -> list[KPIDefinition]:
    """Return KPI definitions matching a customer/staff question (keyword match)."""
    q = query.lower()
    matched = [d for d in KPI_DEFINITIONS if any(kw in q for kw in KPI_KEYWORDS.get(d.key, []))]
    # Generic "kpi / metrics / stats / report / how are we doing" asks for all.
    if not matched and any(
        w in q
        for w in (
            "kpi",
            "metric",
            "stats",
            "statistics",
            "report",
            "how are we doing",
            "performance",
            "numbers",
        )
    ):
        return list(KPI_DEFINITIONS)
    return matched


@dataclass(frozen=True)
class KPIWindow:
    """A parsed natural-language time window: [start, end) + granularity."""

    start: date
    end: date
    granularity: str


_GRANULARITY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("day", ("daily", "day by day", "day-by-day", "by day", "per day", "each day")),
    ("week", ("weekly", "week by week", "week-by-week", "by week", "per week", "each week")),
    (
        "month",
        ("monthly", "month by month", "month-by-month", "by month", "per month", "each month"),
    ),
    ("year", ("yearly", "annually", "annual", "year by year", "by year", "per year", "each year")),
)


def detect_granularity(query: str) -> str | None:
    """Return day/week/month/year when the question asks for a breakdown, else None."""
    q = query.lower()
    for gran, phrases in _GRANULARITY_PATTERNS:
        if any(p in q for p in phrases):
            return gran
    return None


def parse_kpi_window(query: str, today: date | None = None) -> KPIWindow | None:
    """Parse a time window + granularity from a natural-language KPI question.

    Returns None when the question has no time-bucketing language (caller
    keeps the single-period behavior). Supported ranges:

    - explicit year: "booking rate by month for 2024" / "in 2024"
    - "this/last week|month|year"
    - "last N days|weeks|months|years" (N <= 3660 buckets)
    - bare granularity ("monthly booking rate") → trailing window ending
      today: 30 days / 12 weeks / 12 months / 5 years.

    A granularity phrase is required; a bare "last 30 days" without
    day/week/month/year wording returns None.
    """
    q = query.lower()
    now = today or date.today()
    gran = detect_granularity(query)

    m = _re.search(r"\b(19|20)\d{2}\b", q)
    if m and gran:
        year = int(m.group(0))
        return KPIWindow(date(year, 1, 1), date(year + 1, 1, 1), gran)

    m = _re.search(r"\b(this|last)\s+(week|month|year)\b", q)
    if m and gran:
        which, unit = m.group(1), m.group(2)
        if unit == "week":
            monday = now - timedelta(days=now.weekday())
            if which == "last":
                return KPIWindow(monday - timedelta(weeks=1), monday, gran)
            return KPIWindow(monday, monday + timedelta(weeks=1), gran)
        if unit == "month":
            first = now.replace(day=1)
            if which == "last":
                return KPIWindow(_add_months(first, -1), first, gran)
            return KPIWindow(first, _add_months(first, 1), gran)
        if which == "last":
            return KPIWindow(date(now.year - 1, 1, 1), date(now.year, 1, 1), gran)
        return KPIWindow(date(now.year, 1, 1), date(now.year + 1, 1, 1), gran)

    m = _re.search(r"\blast\s+(\d+)\s+(days?|weeks?|months?|years?)\b", q)
    if m and gran:
        n = max(int(m.group(1)), 1)
        unit = m.group(2)
        if unit.startswith("day"):
            return KPIWindow(now - timedelta(days=n), now, gran)
        if unit.startswith("week"):
            return KPIWindow(now - timedelta(weeks=n), now, gran)
        if unit.startswith("month"):
            return KPIWindow(_add_months(now, -n), now, gran)
        return KPIWindow(date(now.year - n, 1, 1), date(now.year, 1, 1), gran)

    if gran:  # bare granularity → trailing window ending today
        if gran == "day":
            return KPIWindow(now - timedelta(days=30), now, gran)
        if gran == "week":
            return KPIWindow(now - timedelta(weeks=12), now, gran)
        if gran == "month":
            return KPIWindow(_add_months(now, -12), now, gran)
        return KPIWindow(date(now.year - 5, 1, 1), date(now.year, 1, 1), gran)
    return None


def _format_value(key: str, v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, dict):
        parts = ", ".join(f"{k}: {vv if vv is not None else 'n/a'}" for k, vv in v.items())
        return parts or "n/a"
    if "rate" in key or "pct" in key or "margin" in key:
        return f"{v}%"
    if "revenue" in key or "profit" in key or "ticket" in key:
        return f"${v:,.2f}" if isinstance(v, float) else f"${v}"
    if "ratio" in key:
        return f"{v}x"
    return str(v)


def format_kpi_answer(values: dict[str, Any], defs: list[KPIDefinition]) -> str:
    """Render matched KPI values as SMS-safe plain text for the prompt."""
    lines = ["=== Company KPIs (current period) ==="]
    for d in defs:
        lines.append(f"{d.name}: {_format_value(d.key, values.get(d.key))}")
    return "\n".join(lines)


def format_kpi_series_answer(
    series: list[dict[str, Any]],
    defs: list[KPIDefinition],
    granularity: str = "month",
) -> str:
    """Render per-bucket KPI values as SMS-safe plain text for the prompt."""
    lines = [f"=== Company KPIs by {granularity} ==="]
    for bucket in series:
        lines.append(f"-- {bucket['label']} --")
        values = bucket.get("values", {})
        for d in defs:
            lines.append(f"{d.name}: {_format_value(d.key, values.get(d.key))}")
    return "\n".join(lines)


# ── Local (Qdrant + Postgres) KPI computation ──────────────────────────
# Zero Jobber calls: aggregates the structured kpi_job / kpi_quote /
# kpi_invoice points written by the hourly sync, plus Aircall call stats
# from Postgres. Anything without a backing data source returns None and
# renders as "n/a" rather than a fabricated number.

JOB_COMPLETED_STATUSES = {"archived", "requires_invoicing"}
QUOTE_WON_STATUSES = {"approved", "converted"}
INVOICE_PAID_STATUSES = {"paid"}
INVOICE_VOID_STATUSES = {"void", "cancelled", "draft"}


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        s = str(v)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def compute_booking_rate(
    jobs: list[dict[str, Any]],
    clients: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
    window_days: int = 14,
) -> float | None:
    """Share of inbound contacts with a job created within `window_days}.

    Joins Postgres inbound contacts (by caller phone hash) to Jobber clients
    (by synced phone hashes) to jobs (by Jobber client id). Contacts are
    inbound SMS senders — labelled as such in the answer, since voice-call
    webhooks are not currently stored.
    """
    hash_to_client: dict[str, str] = {}
    for c in clients:
        cid = str(c.get("client_id", "") or "")
        for h in c.get("phone_hashes", []) or []:
            if h and cid:
                hash_to_client[str(h)] = cid
    jobs_by_client: dict[str, list[datetime]] = {}
    for j in jobs:
        cid = str(j.get("jobber_client_id", "") or "")
        created = _parse_dt(j.get("created_at"))
        if cid and created:
            jobs_by_client.setdefault(cid, []).append(created.replace(tzinfo=None))
    if not contacts:
        return None
    booked = 0
    total = 0
    for ct in contacts:
        first = ct.get("first_seen")
        if not isinstance(first, datetime):
            continue
        first_naive = first.replace(tzinfo=None)
        total += 1
        cids = {hash_to_client[h] for h in ct.get("hashes", []) if h in hash_to_client}
        for cid in cids:
            if any(
                datetime(2000, 1, 1) <= jd
                and (jd - first_naive).days <= window_days
                and (jd - first_naive).days >= 0
                for jd in jobs_by_client.get(cid, [])
            ):
                booked += 1
                break
    return _pct(booked, total)


def compute_local_kpis(
    jobs: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    calls: dict[str, float],
    clients: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    window_days: int = 14,
) -> dict[str, Any]:
    """Compute every KPI backed by local data; unavailable ones are None."""
    completed = [
        j
        for j in jobs
        if str(j.get("status", "")).lower() in JOB_COMPLETED_STATUSES or j.get("completed_at")
    ]
    invoiced_rev = sum(_f(j.get("invoiced_total")) or _f(j.get("total")) for j in completed)
    if not invoiced_rev:
        invoiced_rev = sum(_f(j.get("invoiced_total")) for j in jobs)
    n_completed = len(completed)

    won = [q for q in quotes if str(q.get("status", "")).lower() in QUOTE_WON_STATUSES]
    sold_value = sum(_f(q.get("total")) for q in won)

    now = datetime.now(tz=None)
    unpaid = [
        v
        for v in invoices
        if str(v.get("status", "")).lower() not in INVOICE_PAID_STATUSES | INVOICE_VOID_STATUSES
    ]
    buckets = {
        "current": 0.0,
        "1-30_days_overdue": 0.0,
        "31-60_days_overdue": 0.0,
        "61-90_days_overdue": 0.0,
        "90+_days_overdue": 0.0,
    }
    for v in unpaid:
        amt = _f(v.get("net"))
        due = _parse_dt(v.get("due_date"))
        if due is None:
            buckets["current"] += amt
            continue
        overdue = (now.replace(tzinfo=due.tzinfo) - due).days if due.tzinfo else (now - due).days
        if overdue <= 0:
            buckets["current"] += amt
        elif overdue <= 30:
            buckets["1-30_days_overdue"] += amt
        elif overdue <= 60:
            buckets["31-60_days_overdue"] += amt
        elif overdue <= 90:
            buckets["61-90_days_overdue"] += amt
        else:
            buckets["90+_days_overdue"] += amt
    total_ar = sum(buckets.values())

    labour = sum(_f((j.get("costing") or {}).get("labourCost")) for j in completed)
    materials = sum(
        _f((j.get("costing") or {}).get("lineItemCost"))
        + _f((j.get("costing") or {}).get("expenseCost"))
        for j in completed
    )
    contrib = invoiced_rev - materials - labour  # commissions/fees untracked → partial

    by_source: dict[str, int] = {}
    for j in completed:
        src = str(j.get("source", "") or "UNKNOWN")
        by_source[src] = by_source.get(src, 0) + 1

    total_inbound = _f(calls.get("total_inbound", 0))
    unanswered = _f(calls.get("unanswered", 0))

    booking_rate = compute_booking_rate(
        jobs, clients or [], contacts or [], window_days=window_days
    )

    return {
        # Not backed by local data (cost books, plan-offer tracking, review
        # feed, ad spend) → None = "n/a".
        "lead_booking_rate": booking_rate,
        "in_home_opportunity_conversion_rate": (
            _pct(
                sum(
                    1 for j in completed if (_f(j.get("invoiced_total")) or _f(j.get("total"))) > 0
                ),
                n_completed,
            )
        ),
        "quote_close_rate": _pct(len(won), len(quotes)),
        "sales_to_produced_revenue_ratio": round(sold_value / invoiced_rev, 4)
        if invoiced_rev
        else None,
        "average_ticket": round(invoiced_rev / n_completed, 2) if n_completed else None,
        "callback_rate": None,
        "technician_average_ticket": None,
        "technician_labor_cost_pct": (
            {"all_technicians": _pct(labour, invoiced_rev)} if invoiced_rev else None
        ),
        "on_time_arrival_rate": None,
        "recorded_cost_margin_pct": (
            round((invoiced_rev - materials) / invoiced_rev * 100, 2)
            if invoiced_rev and materials
            else None
        ),
        "contribution_profit": round(contrib, 2) if invoiced_rev else None,
        "contribution_margin_pct": (
            round(contrib / invoiced_rev * 100, 2)
            if invoiced_rev and (materials or labour)
            else None
        ),
        "net_contribution_after_office_pct": None,
        "ar_aging_bucket_share": (
            {k: _pct(v, total_ar) for k, v in buckets.items()} if total_ar else None
        ),
        "maintenance_offer_rate": None,
        "maintenance_close_rate": None,
        "google_review_rate": None,
        "missed_call_rate": _pct(unanswered, total_inbound) if total_inbound else None,
        "lead_to_job_by_channel": (
            {ch: {"completed_jobs": float(n)} for ch, n in by_source.items()} or None
        ),
        "cost_per_qualified_lead": None,
        "total_invoiced_revenue": round(invoiced_rev, 2),
        "total_completed_jobs": float(n_completed),
        "total_quotes": float(len(quotes)),
    }


async def load_local_records(
    qdrant: Any,
    session: Any,
    collection: str,
    company_id: str,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Scroll all kpi_* + client points for a company + contact stats.

    `contacts` are distinct inbound sender numbers (last 90 days) with
    first-seen timestamps and caller phone hashes, minus staff allowlist
    numbers — the denominator for Lead Booking Rate.
    """
    import asyncio as _aio
    from datetime import timedelta

    from qdrant_client.http import models as _qm

    async def _scroll(category: str) -> list[dict[str, Any]]:
        filt = _qm.Filter(
            must=[
                _qm.FieldCondition(key="category", match=_qm.MatchValue(value=category)),
                _qm.FieldCondition(key="company_id", match=_qm.MatchValue(value=company_id)),
            ]
        )
        out: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            pts, offset = await _aio.to_thread(
                qdrant.scroll,
                collection_name=collection,
                scroll_filter=filt,
                limit=500,
                offset=offset,
                with_vectors=False,
            )
            out.extend(p.payload or {} for p in pts)
            if not offset:
                break
        return out

    jobs, quotes, invoices, clients = await _aio.gather(
        _scroll("kpi_job"),
        _scroll("kpi_quote"),
        _scroll("kpi_invoice"),
        _scroll("jobber"),
    )

    from sqlmodel import func, select

    from texponse.db.models import Conversation, ConversationEvent, ConversationEventType

    total_stmt = (
        select(func.count(Conversation.id))
        .where(Conversation.company_id == company_id)
        .where(Conversation.channel == "call")
        .where(Conversation.direction == "inbound")
    )
    total_res = await session.exec(total_stmt)
    total_inbound = float(total_res.one() or 0)
    answered_stmt = (
        select(func.count(func.distinct(ConversationEvent.conversation_id)))
        .join(Conversation, Conversation.id == ConversationEvent.conversation_id)
        .where(Conversation.company_id == company_id)
        .where(ConversationEvent.event_type == ConversationEventType.CALL_ANSWRED)
    )
    answered_res = await session.exec(answered_stmt)
    answered = float(answered_res.one() or 0)

    from texponse.security.guardrails import active_peppers
    from texponse.security.guardrails import phone_hash as _ph

    peppers: list[str] = []
    staff_hashes: set[str] = set()
    if settings is not None:
        peppers = active_peppers(
            settings.secret_key,
            settings.phone_hash_pepper,
            settings.phone_hash_pepper_previous,
        )
        for n in (settings.chunk_security_bypass_numbers or "").split(","):
            digits = "".join(c for c in n if c.isdigit())[-10:]
            if digits:
                staff_hashes.update(_ph(digits, pepper=p) for p in peppers)

    since = datetime.utcnow() - timedelta(days=90)
    contact_stmt = (
        select(
            ConversationEvent.from_number,
            func.min(ConversationEvent.created_at),
        )
        .join(Conversation, Conversation.id == ConversationEvent.conversation_id)
        .where(Conversation.company_id == company_id)
        .where(ConversationEvent.direction == "inbound")
        .where(ConversationEvent.from_number.is_not(None))
        .where(ConversationEvent.created_at >= since)
        .group_by(ConversationEvent.from_number)
    )
    contact_res = await session.exec(contact_stmt)
    contacts: list[dict[str, Any]] = []
    for number, first_seen in contact_res.all():
        hashes = sorted({_ph(str(number), pepper=p) for p in peppers} - {""}) if peppers else []
        if staff_hashes and not set(hashes).isdisjoint(staff_hashes):
            continue
        contacts.append({"hashes": hashes, "first_seen": first_seen})
    return {
        "jobs": jobs,
        "quotes": quotes,
        "invoices": invoices,
        "clients": clients,
        "contacts": contacts,
        "calls": {"total_inbound": total_inbound, "unanswered": max(total_inbound - answered, 0)},
    }


async def load_call_windows(
    session: Any,
    company_id: str,
    windows: list[dict[str, str]],
) -> dict[str, dict[str, float]]:
    """Bucket inbound call stats per window label.

    Two queries total (inbound call conversations + answered events in the
    overall range), bucketed in Python by date. A conversation counts as
    answered when it has a CALL_ANSWRED event; unanswered = total - answered
    (floored at 0). Empty windows report zeros, so `missed_call_rate`
    computes as None (no calls) rather than leaking totals across buckets.
    """
    from sqlmodel import select

    from texponse.db.models import Conversation, ConversationEvent, ConversationEventType

    if not windows:
        return {}
    starts = [date.fromisoformat(w["start"]) for w in windows]
    ends = [date.fromisoformat(w["end"]) for w in windows]
    lo = datetime.combine(min(starts), datetime.min.time())
    hi = datetime.combine(max(ends), datetime.min.time())

    inbound_stmt = (
        select(Conversation.created_at)
        .where(Conversation.company_id == company_id)
        .where(Conversation.channel == "call")
        .where(Conversation.direction == "inbound")
        .where(Conversation.created_at >= lo)
        .where(Conversation.created_at < hi)
    )
    inbound_res = await session.exec(inbound_stmt)
    inbound_dates = [_as_date(v) for v in inbound_res.all()]

    answered_stmt = (
        select(ConversationEvent.created_at)
        .join(Conversation, Conversation.id == ConversationEvent.conversation_id)
        .where(Conversation.company_id == company_id)
        .where(ConversationEvent.event_type == ConversationEventType.CALL_ANSWRED)
        .where(ConversationEvent.created_at >= lo)
        .where(ConversationEvent.created_at < hi)
    )
    answered_res = await session.exec(answered_stmt)
    answered_dates = [_as_date(v) for v in answered_res.all()]

    out: dict[str, dict[str, float]] = {}
    for w, ws, we in zip(windows, starts, ends, strict=True):
        total = sum(1 for d in inbound_dates if d is not None and ws <= d < we)
        answered = sum(1 for d in answered_dates if d is not None and ws <= d < we)
        out[w["label"]] = {
            "total_inbound": float(total),
            "unanswered": float(max(total - answered, 0)),
        }
    return out


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x or default)
    except (TypeError, ValueError):
        return default


def _pct(num: float, den: float) -> float | None:
    return round(num / den * 100, 2) if den else None


# ── Queryable time windows ───────────────────────────────────────────
# Lets callers slice any KPI over an arbitrary [start, end) range split
# into daily / weekly / monthly / yearly buckets. Each bucket is computed
# with the same formulas as compute_local_kpis, filtered to records whose
# relevant timestamp falls inside the bucket.

GRANULARITIES: tuple[str, ...] = ("day", "week", "month", "year")


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    dt = _parse_dt(v)
    return dt.date() if dt else None


def _record_date(
    rec: dict[str, Any], keys: tuple[str, ...] = ("completed_at", "created_at")
) -> date | None:
    for k in keys:
        d = _as_date(rec.get(k))
        if d is not None:
            return d
    return None


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, _monthrange(y, m)[1]))


def generate_windows(
    start: date | datetime | str,
    end: date | datetime | str,
    granularity: str = "month",
) -> list[dict[str, str]]:
    """Split [start, end) into buckets. Returns {label, start, end} (ISO dates).

    Granularities: day, week (Mon-Sun), month, year. Raises ValueError on
    bad input or end <= start.
    """
    g = granularity.lower().strip()
    if g not in GRANULARITIES:
        raise ValueError(f"granularity must be one of {GRANULARITIES}")
    s = _as_date(start)
    e = _as_date(end)
    if s is None or e is None:
        raise ValueError("start and end must be parseable dates")
    if e <= s:
        raise ValueError("end must be after start")
    if g == "week":  # align to Monday
        s = s - timedelta(days=s.weekday())
    elif g == "month":
        s = _month_start(s)
    elif g == "year":
        s = s.replace(month=1, day=1)

    out: list[dict[str, str]] = []
    cur = s
    while cur < e:
        if g == "day":
            nxt = cur + timedelta(days=1)
            label = cur.isoformat()
        elif g == "week":
            nxt = cur + timedelta(weeks=1)
            label = f"{cur.isoformat()}"
        elif g == "month":
            nxt = _add_months(cur, 1)
            label = cur.strftime("%Y-%m")
        else:
            nxt = date(cur.year + 1, 1, 1)
            label = str(cur.year)
        out.append({"label": label, "start": cur.isoformat(), "end": min(nxt, e).isoformat()})
        cur = nxt
        if len(out) > 3660:
            raise ValueError("window too large: too many buckets")
    return out


def _in_window(d: date | None, ws: date, we: date) -> bool:
    return d is not None and ws <= d < we


def compute_kpi_series(
    jobs: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    calls_by_window: dict[str, dict[str, float]] | None = None,
    clients: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    start: date | datetime | str = "",
    end: date | datetime | str = "",
    granularity: str = "month",
    kpi_keys: list[str] | None = None,
    window_days: int = 14,
) -> list[dict[str, Any]]:
    """Compute KPIs per time bucket.

    Records are bucketed by their natural date: jobs by completed_at
    (fallback created_at), quotes by created_at, invoices by due_date
    (fallback created_at). Contacts bucket by first_seen. `calls_by_window`
    optionally maps bucket label -> {"total_inbound": n, "unanswered": m};
    buckets without call stats report call-based KPIs as None.
    """
    windows = generate_windows(start, end, granularity)
    result: list[dict[str, Any]] = []
    for w in windows:
        ws = date.fromisoformat(w["start"])
        we = date.fromisoformat(w["end"])
        w_jobs = [j for j in jobs if _in_window(_record_date(j), ws, we)]
        # Completed-only KPIs should still count jobs completed in-window
        # even if created earlier: also include jobs whose completed_at is
        # in-window regardless of created_at.
        w_quotes = [q for q in quotes if _in_window(_as_date(q.get("created_at")), ws, we)]
        w_invoices = [
            v
            for v in invoices
            if _in_window(_as_date(v.get("due_date")) or _as_date(v.get("created_at")), ws, we)
        ]
        w_contacts = [
            c for c in (contacts or []) if _in_window(_as_date(c.get("first_seen")), ws, we)
        ]
        calls = (calls_by_window or {}).get(w["label"], {"total_inbound": 0, "unanswered": 0})
        booking = compute_booking_rate(jobs, clients or [], w_contacts, window_days=window_days)
        values = compute_local_kpis(
            w_jobs, w_quotes, w_invoices, calls, clients, w_contacts, window_days=window_days
        )
        values["lead_booking_rate"] = booking  # numerator may reference jobs after bucket
        if kpi_keys:
            values = {k: values.get(k) for k in kpi_keys}
        result.append(
            {
                "label": w["label"],
                "start": w["start"],
                "end": w["end"],
                "values": values,
                "counts": {
                    "jobs": len(w_jobs),
                    "quotes": len(w_quotes),
                    "invoices": len(w_invoices),
                    "contacts": len(w_contacts),
                    "inbound_calls": calls.get("total_inbound", 0),
                },
            }
        )
    return result


def compute_kpis(bundle: dict[str, Any]) -> dict[str, float | None | dict[str, float | None]]:
    """Pure math over a normalized data bundle (see JobberKPIService.fetch_bundle)."""
    jobs = bundle.get("jobs", [])
    quotes = bundle.get("quotes", [])
    calls = bundle.get("calls", {})
    techs: dict[str, dict[str, float]] = bundle.get("technicians", {})
    costs = bundle.get("costs", {})
    ar = bundle.get("ar_aging", {})
    maint = bundle.get("maintenance", {})
    reviews = bundle.get("reviews", {})
    channels: dict[str, dict[str, float]] = bundle.get("channels", {})

    completed = [
        j
        for j in jobs
        if str(j.get("status", "")).lower() in {"complete", "completed", "invoiced", "paid"}
    ]
    invoiced_rev = sum(_f(j.get("total")) for j in completed)
    n_completed = len(completed)

    booked_14 = _f(bundle.get("booked_within_14d", 0))
    qualified_callers = _f(calls.get("qualified_inbound_callers", 0))
    in_home = [j for j in completed if j.get("in_home")]
    in_home_paid = [j for j in in_home if _f(j.get("total")) > _f(j.get("trip_charge", 0), 0)]
    won = [q for q in quotes if str(q.get("status", "")).lower() in {"won", "accepted", "approved"}]
    sold_value = sum(_f(q.get("total")) for q in won)

    callbacks = sum(1 for j in completed if j.get("callback_within_30d"))
    on_time = _f(bundle.get("on_time_arrivals", 0))
    total_appts = _f(bundle.get("total_appointments", n_completed))

    materials = _f(costs.get("materials", 0))
    tech_pay = _f(costs.get("technician_pay", sum(t.get("pay", 0) for t in techs.values())))
    commissions = _f(costs.get("commissions", 0))
    fees = _f(costs.get("processing_fees", 0))
    office = _f(costs.get("office_salaries", 0))
    contrib = invoiced_rev - (materials + tech_pay + commissions + fees)

    total_ar = sum(_f(v) for v in ar.values()) or _f(bundle.get("total_ar", 0))
    total_inbound = _f(calls.get("total_inbound", 0))
    unanswered = _f(calls.get("unanswered", 0))

    return {
        "lead_booking_rate": _pct(booked_14, qualified_callers),
        "in_home_opportunity_conversion_rate": _pct(len(in_home_paid), len(in_home)),
        "quote_close_rate": _pct(len(won), len(quotes)),
        "sales_to_produced_revenue_ratio": round(sold_value / invoiced_rev, 4)
        if invoiced_rev
        else None,
        "average_ticket": round(invoiced_rev / n_completed, 2) if n_completed else None,
        "callback_rate": _pct(callbacks, n_completed),
        "technician_average_ticket": {
            tid: (round(t.get("revenue", 0) / t.get("jobs", 0), 2) if t.get("jobs") else None)
            for tid, t in techs.items()
        },
        "technician_labor_cost_pct": {
            tid: _pct(_f(t.get("pay")), _f(t.get("revenue"))) for tid, t in techs.items()
        },
        "on_time_arrival_rate": _pct(on_time, total_appts),
        "recorded_cost_margin_pct": round((invoiced_rev - materials) / invoiced_rev * 100, 2)
        if invoiced_rev
        else None,
        "contribution_profit": round(contrib, 2),
        "contribution_margin_pct": round(contrib / invoiced_rev * 100, 2) if invoiced_rev else None,
        "net_contribution_after_office_pct": round((contrib - office) / invoiced_rev * 100, 2)
        if invoiced_rev
        else None,
        "ar_aging_bucket_share": {k: _pct(_f(v), total_ar) for k, v in ar.items()},
        "maintenance_offer_rate": _pct(_f(maint.get("offered")), _f(maint.get("eligible"))),
        "maintenance_close_rate": _pct(_f(maint.get("sold")), _f(maint.get("offered"))),
        "google_review_rate": _pct(_f(reviews.get("received")), _f(reviews.get("eligible_visits"))),
        "missed_call_rate": _pct(unanswered, total_inbound),
        "lead_to_job_by_channel": {
            ch: _pct(_f(v.get("jobs")), _f(v.get("leads"))) for ch, v in channels.items()
        },
        "cost_per_qualified_lead": {
            ch: (round(_f(v.get("spend")) / _f(v.get("leads")), 2) if _f(v.get("leads")) else None)
            for ch, v in channels.items()
        },
        "total_invoiced_revenue": round(invoiced_rev, 2),
        "total_completed_jobs": float(n_completed),
        "total_quotes": float(len(quotes)),
    }


JOBS_QUERY = """
query KpiJobs($first: Int, $after: String) {
  jobs(first: $first, after: $after) {
    nodes { id title jobStatus total startAt endAt }
    pageInfo { hasNextPage endCursor } totalCount
  }
}"""

QUOTES_QUERY = """
query KpiQuotes($first: Int, $after: String) {
  quotes(first: $first, after: $after) {
    nodes { id title status total createdAt }
    pageInfo { hasNextPage endCursor } totalCount
  }
}"""

INVOICES_QUERY = """
query KpiInvoices($first: Int, $after: String) {
  invoices(first: $first, after: $after) {
    nodes { id invoiceStatus total balance createdAt }
    pageInfo { hasNextPage endCursor } totalCount
  }
}"""


class JobberKPIService:
    """Fetches Jobber aggregates then delegates math to compute_kpis."""

    def __init__(self, graphql: Any, access_token: str) -> None:
        self._graphql = graphql
        self._token = access_token

    async def _paginate(self, query: str, limit: int = 500) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after: str | None = None
        root = "jobs" if "jobs(" in query else ("quotes" if "quotes(" in query else "invoices")
        while len(out) < limit:
            res = await self._graphql(
                query, {"first": min(50, limit - len(out)), "after": after}, self._token
            )
            conn = (res.get("data", {}) or {}).get(root, {}) or {}
            out.extend(conn.get("nodes", []) or [])
            page = conn.get("pageInfo", {}) or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
            if not after:
                break
        return out

    async def fetch_bundle(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> dict[str, Any]:
        _ = (start, end)  # server-side filtering TODO once Jobber exposes date args
        jobs_raw = await self._paginate(JOBS_QUERY)
        quotes_raw = await self._paginate(QUOTES_QUERY)
        invoices_raw = await self._paginate(INVOICES_QUERY)
        jobs = [
            {"status": j.get("jobStatus"), "total": j.get("total"), "in_home": True}
            for j in jobs_raw
        ]
        quotes = [{"status": q.get("status"), "total": q.get("total")} for q in quotes_raw]
        ar: dict[str, float] = {"current": 0.0, "30+": 0.0, "60+": 0.0, "90+": 0.0}
        for inv in invoices_raw:
            if str(inv.get("invoiceStatus", "")).lower() not in {"paid", "void"}:
                ar["current"] += _f(inv.get("balance"))
        return {
            "jobs": jobs,
            "quotes": quotes,
            "calls": {},
            "invoices": {"count": len(invoices_raw)},
            "technicians": {},
            "costs": {},
            "ar_aging": ar,
            "maintenance": {},
            "reviews": {},
            "channels": {},
        }

    async def get_kpis(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> dict[str, Any]:
        bundle = await self.fetch_bundle(start, end)
        return {
            "values": compute_kpis(bundle),
            "definitions": [{**d.__dict__} for d in KPI_DEFINITIONS],
            "counts": {"jobs": len(bundle["jobs"]), "quotes": len(bundle["quotes"])},
        }

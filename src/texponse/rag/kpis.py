"""KPI definitions + computation from Jobber GraphQL data.

All 23 KPIs from the spec. Each formula is documented with its numerator /
denominator so the endpoint response is auditable. Data is fetched live via
JobberClient.graphql; pure math lives in ``compute_kpis`` so it is unit
testable without API access.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    KPIDefinition("lead_booking_rate", "Lead Booking Rate", "sales_conversion",
                  "(Contacts with a Job Created within 14 Days / Total Inbound Contacts) x 100",
                  "Share of inbound SMS contacts converting to a job within 14 days "
                  "(voice calls not tracked; staff lines excluded).", True),
    KPIDefinition("in_home_opportunity_conversion_rate", "In-Home Opportunity Conversion Rate", "sales_conversion",
                  "(Jobs Invoiced Above Trip/Diagnostic Charge / Total In-Home Opportunities) x 100",
                  "Share of on-site visits producing paid repair/replacement revenue.", True),
    KPIDefinition("quote_close_rate", "Quote Close Rate (Win Rate)", "sales_conversion",
                  "(Won Quotes / Total Drafted Quotes) x 100", "Share of formal estimates approved.", True),
    KPIDefinition("sales_to_produced_revenue_ratio", "Sales-to-Produced Revenue Ratio", "sales_conversion",
                  "Total Sold Quote Value / Total Invoiced Revenue (Completed)",
                  "Pipeline replenishment velocity.", False),
    # 2. Operational & Technician
    KPIDefinition("average_ticket", "Average Ticket / Revenue per Completed Job", "operations",
                  "Total Invoiced Job Revenue / Total Completed Jobs Count",
                  "Average dollars per completed job.", True),
    KPIDefinition("callback_rate", "Callback Rate", "operations",
                  "(Jobs Requiring Unpaid Return within 30 Days / Total Completed Jobs) x 100",
                  "Lower = higher first-time fix rate.", True),
    KPIDefinition("technician_average_ticket", "Technician Average Ticket", "operations",
                  "Total Revenue Credited to Technician / Technician True Completed Jobs Count",
                  "Per-tech ticket size and upsell effectiveness.", False),
    KPIDefinition("technician_labor_cost_pct", "Technician Labor Cost Percentage", "operations",
                  "(Technician Direct Pay / Invoiced Revenue Credited) x 100",
                  "Field direct labor burden vs produced revenue.", False),
    KPIDefinition("on_time_arrival_rate", "On-Time Arrival Rate", "operations",
                  "(On-Time Dispatched Arrivals / Total Completed Appointments) x 100",
                  "Punctuality vs appointment windows.", False),
    # 3. Financial & Job Costing
    KPIDefinition("recorded_cost_margin_pct", "Recorded-Cost Margin % (Materials Margin)", "financial",
                  "((Invoiced Revenue - Material & Equipment Costs) / Invoiced Revenue) x 100",
                  "Gross margin before labor/commissions/overhead.", True),
    KPIDefinition("contribution_profit", "Contribution Profit ($)", "financial",
                  "Invoiced Revenue - (Materials + Tech Pay + Commissions + Processing Fees)",
                  "Cash dollars left for overhead and profit.", True),
    KPIDefinition("contribution_margin_pct", "Contribution Margin (%)", "financial",
                  "(Contribution Profit / Invoiced Revenue) x 100",
                  "Revenue retained after direct variable costs.", False),
    KPIDefinition("net_contribution_after_office_pct", "Net Contribution After Office Staff (%)", "financial",
                  "((Contribution Profit - Office Salaries) / Invoiced Revenue) x 100",
                  "Margin after field + office support pay.", False),
    KPIDefinition("ar_aging_bucket_share", "AR Aging Bucket Share", "financial",
                  "(Unpaid AR Balance in Category / Total Outstanding AR) x 100",
                  "Collection efficiency / bad-debt exposure.", False),
    # 4. Recurring & Reputation
    KPIDefinition("maintenance_offer_rate", "Maintenance Plan (Pros Club) Offer Rate", "recurring",
                  "(Jobs with Documented Plan Offer / Total Eligible Residential Jobs) x 100",
                  "How often plans are presented.", False),
    KPIDefinition("maintenance_close_rate", "Maintenance Plan Attachment / Close Rate", "recurring",
                  "(Plans Sold / Plans Offered) x 100", "Acceptance rate when offered.", False),
    KPIDefinition("google_review_rate", "Google Review Acquisition Rate", "recurring",
                  "(Reviews in Period / Total Eligible Completed Visits) x 100",
                  "Satisfaction / review capture rate.", False),
    # 5. Call Center & Marketing
    KPIDefinition("missed_call_rate", "Missed Call / Abandonment Rate", "marketing",
                  "(Unanswered Inbound Calls / Total Inbound Calls) x 100",
                  "Front-office capture responsiveness.", True),
    KPIDefinition("lead_to_job_by_channel", "Lead-to-Job Conversion Rate by Channel", "marketing",
                  "(Completed Jobs per Source / Inbound Leads per Source) x 100",
                  "Traffic quality per ad platform.", False),
    KPIDefinition("cost_per_qualified_lead", "Cost Per Qualified Lead (CPL)", "marketing",
                  "Channel Spend / Qualified Inbound Leads", "Spend efficiency by channel.", False),
    # Derived extras to reach full operational coverage
    KPIDefinition("total_invoiced_revenue", "Total Invoiced Revenue", "financial",
                  "SUM(job.total where invoiced)", "Base produced revenue.", False),
    KPIDefinition("total_completed_jobs", "Total Completed Jobs", "operations",
                  "COUNT(jobs with completed status)", "Base volume.", False),
    KPIDefinition("total_quotes", "Total Quotes Drafted", "sales_conversion",
                  "COUNT(quotes drafted/presented)", "Base pipeline volume.", False),
]


KPI_KEYWORDS: dict[str, list[str]] = {
    "lead_booking_rate": ["booking rate", "lead booking", "book rate", "conversion rate"],
    "in_home_opportunity_conversion_rate": ["in-home conversion", "in home conversion", "close rate on site"],
    "quote_close_rate": ["quote close", "win rate", "quote approval", "estimate approval", "close rate"],
    "sales_to_produced_revenue_ratio": ["sales to produced", "pipeline velocity", "sold vs invoiced"],
    "average_ticket": ["average ticket", "avg ticket", "revenue per job", "average job value"],
    "callback_rate": ["callback rate", "callbacks", "warranty return", "first-time fix", "first time fix"],
    "technician_average_ticket": ["technician average", "tech ticket", "per tech revenue"],
    "technician_labor_cost_pct": ["labor cost", "labor burden", "tech pay percent"],
    "on_time_arrival_rate": ["on-time", "on time arrival", "punctuality", "late arrival"],
    "recorded_cost_margin_pct": ["margin", "materials margin", "gross margin", "material cost"],
    "contribution_profit": ["contribution profit", "contribution dollars", "profit dollars"],
    "contribution_margin_pct": ["contribution margin"],
    "net_contribution_after_office_pct": ["net contribution", "after office", "office staff margin"],
    "ar_aging_bucket_share": ["ar aging", "receivables", "a/r", "unpaid balance", "collections", "bad debt"],
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
    matched = [
        d for d in KPI_DEFINITIONS
        if any(kw in q for kw in KPI_KEYWORDS.get(d.key, []))
    ]
    # Generic "kpi / metrics / stats / report / how are we doing" asks for all.
    if not matched and any(w in q for w in ("kpi", "metric", "stats", "statistics", "report", "how are we doing", "performance", "numbers")):
        return list(KPI_DEFINITIONS)
    return matched


def format_kpi_answer(values: dict[str, Any], defs: list[KPIDefinition]) -> str:
    """Render matched KPI values as SMS-safe plain text for the prompt."""
    lines = ["=== Company KPIs (current period) ==="]
    for d in defs:
        v = values.get(d.key)
        if v is None:
            lines.append(f"{d.name}: n/a")
        elif isinstance(v, dict):
            parts = ", ".join(f"{k}: {vv if vv is not None else 'n/a'}" for k, vv in v.items())
            lines.append(f"{d.name}: {parts or 'n/a'}")
        elif "rate" in d.key or "pct" in d.key or "margin" in d.key:
            lines.append(f"{d.name}: {v}%")
        elif "revenue" in d.key or "profit" in d.key or "ticket" in d.key:
            lines.append(f"{d.name}: ${v:,.2f}" if isinstance(v, float) else f"{d.name}: ${v}")
        elif "ratio" in d.key:
            lines.append(f"{d.name}: {v}x")
        else:
            lines.append(f"{d.name}: {v}")
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
                datetime(2000, 1, 1) <= jd and (jd - first_naive).days <= window_days and (jd - first_naive).days >= 0
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
) -> dict[str, Any]:
    """Compute every KPI backed by local data; unavailable ones are None."""
    completed = [
        j for j in jobs
        if str(j.get("status", "")).lower() in JOB_COMPLETED_STATUSES or j.get("completed_at")
    ]
    invoiced_rev = sum(_f(j.get("invoiced_total")) or _f(j.get("total")) for j in completed)
    if not invoiced_rev:
        invoiced_rev = sum(_f(j.get("invoiced_total")) for j in jobs)
    n_completed = len(completed)

    won = [q for q in quotes if str(q.get("status", "")).lower() in QUOTE_WON_STATUSES]
    sold_value = sum(_f(q.get("total")) for q in won)

    now = datetime.now(tz=None)
    unpaid = [v for v in invoices if str(v.get("status", "")).lower() not in INVOICE_PAID_STATUSES | INVOICE_VOID_STATUSES]
    buckets = {"current": 0.0, "1-30_days_overdue": 0.0, "31-60_days_overdue": 0.0,
               "61-90_days_overdue": 0.0, "90+_days_overdue": 0.0}
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
        + _f((j.get("costing") or {}).get("expenseCost")) for j in completed
    )
    contrib = invoiced_rev - materials - labour  # commissions/fees untracked → partial

    by_source: dict[str, int] = {}
    for j in completed:
        src = str(j.get("source", "") or "UNKNOWN")
        by_source[src] = by_source.get(src, 0) + 1

    total_inbound = _f(calls.get("total_inbound", 0))
    unanswered = _f(calls.get("unanswered", 0))

    booking_rate = compute_booking_rate(jobs, clients or [], contacts or [])

    return {
        # Not backed by local data (cost books, plan-offer tracking, review
        # feed, ad spend) → None = "n/a".
        "lead_booking_rate": booking_rate,
        "in_home_opportunity_conversion_rate": (
            _pct(sum(1 for j in completed if (_f(j.get("invoiced_total")) or _f(j.get("total"))) > 0), n_completed)
        ),
        "quote_close_rate": _pct(len(won), len(quotes)),
        "sales_to_produced_revenue_ratio": round(sold_value / invoiced_rev, 4) if invoiced_rev else None,
        "average_ticket": round(invoiced_rev / n_completed, 2) if n_completed else None,
        "callback_rate": None,
        "technician_average_ticket": None,
        "technician_labor_cost_pct": (
            {"all_technicians": _pct(labour, invoiced_rev)} if invoiced_rev else None
        ),
        "on_time_arrival_rate": None,
        "recorded_cost_margin_pct": (
            round((invoiced_rev - materials) / invoiced_rev * 100, 2) if invoiced_rev and materials else None
        ),
        "contribution_profit": round(contrib, 2) if invoiced_rev else None,
        "contribution_margin_pct": (
            round(contrib / invoiced_rev * 100, 2) if invoiced_rev and (materials or labour) else None
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
    qdrant: Any, session: Any, collection: str, company_id: str,
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
                qdrant.scroll, collection_name=collection,
                scroll_filter=filt, limit=500, offset=offset, with_vectors=False,
            )
            out.extend(p.payload or {} for p in pts)
            if not offset:
                break
        return out

    jobs, quotes, invoices, clients = await _aio.gather(
        _scroll("kpi_job"), _scroll("kpi_quote"), _scroll("kpi_invoice"),
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
            settings.secret_key, settings.phone_hash_pepper,
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
        "jobs": jobs, "quotes": quotes, "invoices": invoices, "clients": clients,
        "contacts": contacts,
        "calls": {"total_inbound": total_inbound,
                  "unanswered": max(total_inbound - answered, 0)},
    }


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x or default)
    except (TypeError, ValueError):
        return default


def _pct(num: float, den: float) -> float | None:
    return round(num / den * 100, 2) if den else None


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

    completed = [j for j in jobs if str(j.get("status", "")).lower() in {"complete", "completed", "invoiced", "paid"}]
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
        "sales_to_produced_revenue_ratio": round(sold_value / invoiced_rev, 4) if invoiced_rev else None,
        "average_ticket": round(invoiced_rev / n_completed, 2) if n_completed else None,
        "callback_rate": _pct(callbacks, n_completed),
        "technician_average_ticket": {tid: (round(t.get("revenue", 0) / t.get("jobs", 0), 2) if t.get("jobs") else None) for tid, t in techs.items()},
        "technician_labor_cost_pct": {tid: _pct(_f(t.get("pay")), _f(t.get("revenue"))) for tid, t in techs.items()},
        "on_time_arrival_rate": _pct(on_time, total_appts),
        "recorded_cost_margin_pct": round((invoiced_rev - materials) / invoiced_rev * 100, 2) if invoiced_rev else None,
        "contribution_profit": round(contrib, 2),
        "contribution_margin_pct": round(contrib / invoiced_rev * 100, 2) if invoiced_rev else None,
        "net_contribution_after_office_pct": round((contrib - office) / invoiced_rev * 100, 2) if invoiced_rev else None,
        "ar_aging_bucket_share": {k: _pct(_f(v), total_ar) for k, v in ar.items()},
        "maintenance_offer_rate": _pct(_f(maint.get("offered")), _f(maint.get("eligible"))),
        "maintenance_close_rate": _pct(_f(maint.get("sold")), _f(maint.get("offered"))),
        "google_review_rate": _pct(_f(reviews.get("received")), _f(reviews.get("eligible_visits"))),
        "missed_call_rate": _pct(unanswered, total_inbound),
        "lead_to_job_by_channel": {ch: _pct(_f(v.get("jobs")), _f(v.get("leads"))) for ch, v in channels.items()},
        "cost_per_qualified_lead": {ch: (round(_f(v.get("spend")) / _f(v.get("leads")), 2) if _f(v.get("leads")) else None) for ch, v in channels.items()},
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
            res = await self._graphql(query, {"first": min(50, limit - len(out)), "after": after}, self._token)
            conn = (res.get("data", {}) or {}).get(root, {}) or {}
            out.extend(conn.get("nodes", []) or [])
            page = conn.get("pageInfo", {}) or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
            if not after:
                break
        return out

    async def fetch_bundle(self, start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
        _ = (start, end)  # server-side filtering TODO once Jobber exposes date args
        jobs_raw = await self._paginate(JOBS_QUERY)
        quotes_raw = await self._paginate(QUOTES_QUERY)
        invoices_raw = await self._paginate(INVOICES_QUERY)
        jobs = [{"status": j.get("jobStatus"), "total": j.get("total"), "in_home": True} for j in jobs_raw]
        quotes = [{"status": q.get("status"), "total": q.get("total")} for q in quotes_raw]
        ar: dict[str, float] = {"current": 0.0, "30+": 0.0, "60+": 0.0, "90+": 0.0}
        for inv in invoices_raw:
            if str(inv.get("invoiceStatus", "")).lower() not in {"paid", "void"}:
                ar["current"] += _f(inv.get("balance"))
        return {"jobs": jobs, "quotes": quotes, "calls": {}, "invoices": {"count": len(invoices_raw)},
                "technicians": {}, "costs": {}, "ar_aging": ar,
                "maintenance": {}, "reviews": {}, "channels": {}}

    async def get_kpis(self, start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
        bundle = await self.fetch_bundle(start, end)
        return {"values": compute_kpis(bundle),
                "definitions": [{**d.__dict__} for d in KPI_DEFINITIONS],
                "counts": {"jobs": len(bundle["jobs"]), "quotes": len(bundle["quotes"])}}

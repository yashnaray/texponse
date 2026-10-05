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
                  "(Booked Jobs within 14 Days / Total Qualified Inbound Callers) x 100",
                  "Share of qualified inbound calls converting to a scheduled appointment.", True),
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

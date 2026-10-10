from __future__ import annotations

from datetime import date, datetime

from texponse.rag.kpis import (
    KPI_DEFINITIONS,
    compute_kpi_series,
    detect_granularity,
    format_kpi_series_answer,
    generate_windows,
    parse_kpi_window,
)


def test_detect_granularity():
    assert detect_granularity("What is our monthly booking rate?") == "month"
    assert detect_granularity("Daily revenue for last week") == "day"
    assert detect_granularity("Booking rate by year") == "year"
    assert detect_granularity("Average ticket by week") == "week"
    assert detect_granularity("What is our booking rate?") is None


def test_parse_explicit_year():
    w = parse_kpi_window("booking rate by month for 2024", today=date(2025, 6, 1))
    assert w is not None
    assert (w.start, w.end, w.granularity) == (date(2024, 1, 1), date(2025, 1, 1), "month")


def test_parse_this_last_month_year():
    w = parse_kpi_window("daily revenue for last month", today=date(2025, 6, 15))
    assert w is not None
    assert (w.start, w.end, w.granularity) == (date(2025, 5, 1), date(2025, 6, 1), "day")
    w = parse_kpi_window("yearly totals this year", today=date(2025, 6, 15))
    assert w is not None
    assert (w.start, w.end, w.granularity) == (date(2025, 1, 1), date(2026, 1, 1), "year")


def test_parse_last_n_units():
    w = parse_kpi_window("booking rate by week over the last 4 weeks", today=date(2025, 6, 15))
    assert w is not None
    assert w.granularity == "week"
    assert (w.end - w.start).days == 28


def test_parse_bare_granularity_trailing_window():
    w = parse_kpi_window("monthly booking rate", today=date(2025, 6, 15))
    assert w is not None
    assert w.granularity == "month" and w.end == date(2025, 6, 15)


def test_parse_no_window_language():
    assert parse_kpi_window("What is our booking rate?") is None
    # Range without bucketing words stays single-period.
    assert parse_kpi_window("Revenue in the last 30 days", today=date(2025, 6, 15)) is None


def test_series_booking_rate_per_bucket():
    jobs = [
        {
            "status": "archived",
            "total": 100,
            "invoiced_total": 100,
            "created_at": "2024-01-05T00:00:00",
            "completed_at": "2024-01-05T00:00:00",
            "jobber_client_id": "c1",
            "costing": {},
        },
        {
            "status": "archived",
            "total": 300,
            "invoiced_total": 300,
            "created_at": "2024-02-10T00:00:00",
            "completed_at": "2024-02-10T00:00:00",
            "jobber_client_id": "c1",
            "costing": {},
        },
    ]
    series = compute_kpi_series(
        jobs,
        [],
        [],
        None,
        [{"client_id": "c1", "phone_hashes": ["h1"]}],
        [
            {"hashes": ["h1"], "first_seen": datetime(2024, 1, 1)},
            {"hashes": ["h1"], "first_seen": datetime(2024, 2, 1)},
        ],
        start="2024-01-01",
        end="2024-03-01",
        granularity="month",
        kpi_keys=["lead_booking_rate", "average_ticket"],
    )
    assert [b["label"] for b in series] == ["2024-01", "2024-02"]
    assert series[0]["values"]["lead_booking_rate"] == 100.0
    assert series[0]["values"]["average_ticket"] == 100.0
    assert series[1]["values"]["average_ticket"] == 300.0


def test_format_series_answer_labels_and_values():
    defs = [d for d in KPI_DEFINITIONS if d.key in ("lead_booking_rate", "average_ticket")]
    series = [
        {
            "label": "2024-01",
            "start": "2024-01-01",
            "end": "2024-02-01",
            "values": {"lead_booking_rate": 50.0, "average_ticket": None},
            "counts": {},
        }
    ]
    out = format_kpi_series_answer(series, defs, "month")
    assert "by month" in out
    assert "2024-01" in out
    assert "50.0%" in out
    assert "n/a" in out


def test_generate_windows_validation():
    import pytest

    with pytest.raises(ValueError):
        generate_windows("2024-02-01", "2024-01-01", "month")
    with pytest.raises(ValueError):
        generate_windows("2024-01-01", "2024-02-01", "fortnight")

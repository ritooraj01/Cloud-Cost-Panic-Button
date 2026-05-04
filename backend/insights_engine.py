"""
Insights Engine — Central Orchestrator
Combines all analyzers and utils into ONE clean output.
The FastAPI endpoint calls only this; keeps routing layer thin.
"""

from backend.analyzers import breakdown_analyzer, spike_detector, cost_drivers, waste_detector
from backend.analyzers import usage_breakdown_analyzer
from backend.utils import translator, suggestions


def generate(parsed: dict) -> dict:
    """
    Args:
        parsed: output of aws_parser.parse()
    Returns full insights payload consumed by the frontend.
    """
    records = parsed["records"]
    days_count = parsed["days_count"]
    currency = parsed.get("currency", "USD")

    # ---- Run all analyzers ----------------------------------------------
    breakdown = breakdown_analyzer.analyze(records)
    spike = spike_detector.detect(records, days_count)
    drivers = cost_drivers.find(records, days_count)
    waste = waste_detector.detect(records, days_count)
    usage_breakdown = usage_breakdown_analyzer.analyze(records)

    total_cost = breakdown["total_cost"]
    period = breakdown["period_comparison"]
    period_type = breakdown["period_type"]
    period_label = breakdown["period_label"]
    prev_period_label = breakdown["prev_period_label"]

    # ---- Translate to human language ------------------------------------
    summary_human = translator.translate_summary(
        period, total_cost, currency, period_label, prev_period_label
    )

    for d in drivers:
        d["human_text"] = translator.translate_driver(d, total_cost, currency)

    for w in waste:
        w["human_text"] = translator.translate_waste_signal(w, total_cost, currency)

    # ---- Build suggestions ----------------------------------------------
    sugg = suggestions.build(
        waste, spike, total_cost,
        records=records, currency=currency,
        usage_breakdown=usage_breakdown,
    )

    # ---- Compute total potential savings --------------------------------
    total_savings = sum(s["savings_inr"] for s in sugg)
    # If usage_breakdown has avoidable cost estimate, use whichever is higher
    if usage_breakdown.get("available") and usage_breakdown.get("total_avoidable_usd", 0) > 0:
        ubd_savings_inr = round(usage_breakdown["total_avoidable_usd"] * (83.0 if currency == "USD" else 1.0), 0)
        total_savings = max(total_savings, ubd_savings_inr)

    # ---- Compose final payload ------------------------------------------
    return {
        # Summary card data
        "summary": {
            "last_7_days_inr": summary_human["last_7_days_inr"],
            "previous_7_days_inr": summary_human["previous_7_days_inr"],
            "change_pct": summary_human["change_pct"],
            "change_amount_inr": summary_human["change_amount_inr"],
            "trend_emoji": summary_human["trend_emoji"],
            "trend_label": summary_human["trend_label"],
            "narrative": summary_human["narrative"],
            "total_potential_savings_inr": round(total_savings, 0),
            "spike_detected": spike["spike_detected"],
            "spike_magnitude": spike.get("spike_magnitude"),
            "period_label": summary_human["period_label"],
            "prev_period_label": summary_human["prev_period_label"],
        },

        # Top 3 cost drivers (hero section)
        "top_drivers": drivers,

        # Spike details
        "spike": {
            "detected": spike["spike_detected"],
            "insufficient_data": spike["insufficient_data"],
            "reason": spike["reason"],
            "overall_change_pct": spike["overall_change_pct"],
            "magnitude": spike.get("spike_magnitude"),
            "affected_services": spike["affected_services"],
            "affected_regions": spike["affected_regions"],
        },

        # Waste signals
        "waste_signals": waste,

        # Actionable suggestions
        "suggestions": sugg,

        # Chart data
        "charts": {
            "top_services": breakdown["top_services"],
            "daily_trend": breakdown["daily_trend"],
            "region_breakdown": breakdown["region_breakdown"],
        },

        # Usage-type detailed breakdown (available when usage_type CSV is uploaded)
        "usage_breakdown": usage_breakdown,

        # Metadata
        "meta": {
            "days_analyzed": days_count,
            "date_range": parsed["date_range"],
            "services_found": parsed["services"],
            "regions_found": parsed["regions"],
            "parse_warnings": parsed.get("errors", []),
            "currency": currency,
            "period_type": period_type,
        },
    }

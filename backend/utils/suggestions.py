"""
Smart Suggestions Engine
Maps waste signals and spike data → actionable recommendations.
Each suggestion includes: action, impact (₹/month), confidence, signal_type.
Language is conservative: "potential savings UP TO" — never overpromise.
"""

from collections import defaultdict

# Approximate monthly multiplier from weekly costs (4.33 weeks/month)
WEEKLY_TO_MONTHLY = 4.33

# Service-specific optimization tips for single-period (monthly) data
_SERVICE_TIPS = {
    "EC2-Instances": {
        "action": "Switch to Reserved Instances or Savings Plans for always-on workloads",
        "detail": (
            "EC2 On-Demand instances cost up to 40% more than Reserved Instances. "
            "If these instances run 24/7, a 1-year Reserved Instance (no upfront) "
            "is a safe way to cut your biggest line item significantly."
        ),
        "savings_pct": 0.30,
        "confidence": "MEDIUM",
    },
    "EC2-Other": {
        "action": "Audit EC2 ancillary charges — EBS volumes, snapshots, data transfer",
        "detail": (
            "EC2-Other covers EBS storage, snapshots, and data transfer. "
            "In the AWS Console go to EC2 → Volumes and filter by 'available' state — "
            "those are unattached disks you're paying for with no benefit. "
            "Also delete outdated snapshots older than 30 days."
        ),
        "savings_pct": 0.20,
        "confidence": "MEDIUM",
    },
    "CloudWatch": {
        "action": "Set CloudWatch log retention policies and remove unused metrics",
        "detail": (
            "CloudWatch charges for log ingestion, storage, and custom metrics. "
            "Set each log group's retention to 7–30 days (AWS Console → CloudWatch → Log Groups → Edit retention). "
            "Remove dashboards and alarms you no longer use."
        ),
        "savings_pct": 0.40,
        "confidence": "MEDIUM",
    },
    "Security Hub": {
        "action": "Disable unused Security Hub standards and remove inactive member accounts",
        "detail": (
            "Security Hub charges per account and per security finding. "
            "Disable compliance standards you don't need (e.g., PCI DSS, NIST if not required) "
            "and remove any sandbox or inactive accounts from your Hub."
        ),
        "savings_pct": 0.35,
        "confidence": "MEDIUM",
    },
    "Elastic Load Balancing": {
        "action": "Terminate idle load balancers — they charge hourly even with zero traffic",
        "detail": (
            "Every ALB/NLB has an hourly charge regardless of traffic. "
            "Go to EC2 → Load Balancers, check each one's request count metrics in CloudWatch, "
            "and delete any with near-zero traffic over the past 7 days."
        ),
        "savings_pct": 0.50,
        "confidence": "MEDIUM",
    },
    "WAF": {
        "action": "Review WAF WebACL rules and consolidate managed rule groups",
        "detail": (
            "WAF charges per WebACL ($5/month), per rule, and per 1M requests. "
            "Review if all managed rule groups are actually needed and whether "
            "multiple WebACLs can be consolidated."
        ),
        "savings_pct": 0.25,
        "confidence": "LOW",
    },
    "OpenSearch Service": {
        "action": "Right-size or schedule OpenSearch cluster shutdowns for dev environments",
        "detail": (
            "OpenSearch clusters run 24/7 by default. For non-production environments, "
            "consider downsizing the instance type or using auto-tune. "
            "If it's a dev cluster, schedule off-hours shutdowns."
        ),
        "savings_pct": 0.25,
        "confidence": "MEDIUM",
    },
    "Redshift": {
        "action": "Enable Redshift pause/resume for non-production clusters",
        "detail": (
            "Redshift clusters can be paused when not in use — paused clusters save ~75% "
            "vs running costs. For dev/test workloads, automate pause/resume with a CloudWatch schedule."
        ),
        "savings_pct": 0.40,
        "confidence": "MEDIUM",
    },
    "ElastiCache": {
        "action": "Review ElastiCache node sizing and consider Reserved Nodes",
        "detail": (
            "ElastiCache Reserved Nodes can save up to 45% over On-Demand pricing. "
            "Also check your cluster's cache hit rate — a low hit rate might indicate "
            "the cluster is too small and causing expensive DB fallbacks, or too large and wasteful."
        ),
        "savings_pct": 0.30,
        "confidence": "MEDIUM",
    },
    "S3": {
        "action": "Enable S3 Intelligent-Tiering and set lifecycle policies for old objects",
        "detail": (
            "S3 Intelligent-Tiering automatically moves infrequently accessed objects to cheaper storage tiers. "
            "Set lifecycle policies to transition data older than 90 days to S3 Glacier Instant Retrieval "
            "for long-term cost reduction."
        ),
        "savings_pct": 0.20,
        "confidence": "MEDIUM",
    },
    "Key Management Service": {
        "action": "Audit KMS keys and delete unused customer-managed keys",
        "detail": (
            "Each KMS customer-managed key (CMK) costs $1/month plus $0.03 per 10,000 API calls. "
            "Go to KMS → Customer managed keys and check Last used date — "
            "keys not used in 30+ days are candidates for deletion."
        ),
        "savings_pct": 0.50,
        "confidence": "LOW",
    },
    "Elastic Load Balancing": {
        "action": "Audit and remove idle load balancers",
        "detail": (
            "Load balancers incur an hourly charge even when idle. "
            "Review all ALBs/NLBs in the console and delete those with no active target groups or traffic."
        ),
        "savings_pct": 0.40,
        "confidence": "MEDIUM",
    },
}


def _build_service_suggestions(records: list, currency: str = "USD") -> list:
    """
    Generate optimization suggestions based on service spending patterns.
    Used as fallback when waste signals are unavailable (< 7 days of data).
    """
    scale = 83.0 if currency == "USD" else 1.0

    service_totals: dict[str, float] = defaultdict(float)
    for r in records:
        service_totals[r["service"]] += r["cost"]

    suggestions = []
    MIN_COST_USD = 2.0  # only suggest for services costing > $2

    for svc, cost in sorted(service_totals.items(), key=lambda x: x[1], reverse=True):
        if cost < MIN_COST_USD:
            continue
        tip = _SERVICE_TIPS.get(svc)
        if not tip:
            continue

        monthly_savings = round(cost * tip["savings_pct"] * scale, 0)
        if monthly_savings < 100:
            continue

        copyable = (
            f"Action: {tip['action']} — potential savings up to "
            f"₹{monthly_savings:,.0f}/month ({tip['confidence'].title()} confidence)"
        )
        suggestions.append(
            {
                "action": tip["action"],
                "detail": tip["detail"],
                "savings_inr": monthly_savings,
                "confidence": tip["confidence"],
                "signal_type": "optimization_opportunity",
                "copyable_text": copyable,
            }
        )
        if len(suggestions) >= 5:
            break

    return suggestions


def _build_usage_type_suggestions(usage_breakdown: dict, currency: str = "USD") -> list:
    """
    Generate precise suggestions from usage_breakdown_analyzer output.
    These are higher quality than the generic service tips because they
    are based on actual usage type patterns.
    """
    if not usage_breakdown or not usage_breakdown.get("available"):
        return []

    scale = 83.0 if currency == "USD" else 1.0
    suggestions = []

    for item in usage_breakdown.get("waste_items", []):
        avoidable_usd = item["cost_usd"] * item["avoidable_pct"]
        savings_inr = round(avoidable_usd * scale, 0)
        if savings_inr < 500:
            continue

        copyable = (
            f"Action: {item['action']} — potential savings up to "
            f"₹{savings_inr:,.0f}/month ({item['confidence'].title()} confidence)"
        )
        suggestions.append({
            "action": item["action"],
            "detail": item["description"],
            "savings_inr": savings_inr,
            "confidence": item["confidence"],
            "signal_type": item["type"],
            "copyable_text": copyable,
        })

    # Sort by savings descending
    suggestions.sort(key=lambda s: s["savings_inr"], reverse=True)
    return suggestions


def build(waste_signals: list[dict], spike_data: dict, total_cost: float,
          records: list = None, currency: str = "USD",
          usage_breakdown: dict = None) -> list[dict]:
    """
    Returns a list of suggestion dicts:
      {
        "action": str,
        "detail": str,
        "savings_inr": float,          # ₹/month estimate
        "confidence": "HIGH" | "MEDIUM" | "LOW",
        "signal_type": str,
        "copyable_text": str,           # human-readable for copy button
      }
    """
    suggestions = []
    MIN_SAVINGS_INR = 500  # only include if potential monthly savings > ₹500

    # USD→INR scaling
    scale = 83.0 if currency == "USD" else 1.0

    for signal in waste_signals:
        raw_savings_weekly = signal.get("potential_savings_inr", 0) * scale
        monthly_est = round(raw_savings_weekly * WEEKLY_TO_MONTHLY, 0)

        if monthly_est < MIN_SAVINGS_INR and signal["signal_type"] not in (
            "data_transfer_spike",
        ):
            continue

        confidence = signal["confidence"]
        sig_type = signal["signal_type"]
        services = signal.get("services_involved", [])
        svc_label = services[0] if services else "resource"

        if sig_type == "constant_cost":
            action = f"Review always-on {svc_label} usage and schedule downtime or right-size"
            detail = (
                f"{svc_label} shows a flat daily cost pattern — indicative of a resource "
                f"running continuously. Consider stopping during off-hours or switching to a "
                f"smaller instance/tier."
            )
            copyable = (
                f"Action: Review {svc_label} usage — potential savings up to "
                f"₹{monthly_est:,.0f}/month ({confidence.title()} confidence)"
            )

        elif sig_type == "ebs_without_ec2":
            action = "Audit EBS volumes for unattached or orphaned storage"
            detail = (
                "Storage billing without matching compute suggests volumes may be unattached. "
                "Go to EC2 → Volumes in AWS Console and check for 'available' state volumes."
            )
            copyable = (
                f"Action: Audit EBS volumes — potential savings up to "
                f"₹{monthly_est:,.0f}/month ({confidence.title()} confidence)"
            )

        elif sig_type == "data_transfer_spike":
            action = "Investigate unexpected data transfer usage"
            detail = (
                "A sudden jump in data transfer costs can signal API misconfiguration, "
                "accidental cross-region replication, or unexpected traffic. Check CloudWatch "
                "for network metrics."
            )
            monthly_est = max(monthly_est, MIN_SAVINGS_INR)
            copyable = (
                f"Action: Investigate data transfer spike — potential savings vary "
                f"({confidence.title()} confidence)"
            )

        elif sig_type == "regional_concentration":
            action = "Confirm all regional resources are intentionally active"
            detail = (
                "High cost concentration in one region is fine if intentional, but verify "
                "no test or dev resources are running in unexpected regions."
            )
            copyable = (
                f"Action: Audit regional spend allocation ({confidence.title()} confidence)"
            )
        else:
            continue

        suggestions.append(
            {
                "action": action,
                "detail": detail,
                "savings_inr": monthly_est,
                "confidence": confidence,
                "signal_type": sig_type,
                "copyable_text": copyable,
            }
        )

    # ---- If spike detected, add a generic investigate suggestion ---------
    if spike_data.get("spike_detected") and spike_data.get("affected_services"):
        top_svc = spike_data["affected_services"][0]
        svc_name = top_svc["service"]
        change_amt = round(top_svc.get("change_amount", 0) * scale * WEEKLY_TO_MONTHLY, 0)
        if change_amt >= MIN_SAVINGS_INR:
            suggestions.append(
                {
                    "action": f"Investigate {svc_name} usage spike and consider scaling down",
                    "detail": (
                        f"{svc_name} shows a {top_svc['change_pct']:+.0f}% cost increase. "
                        "If this is unexpected, review running resources in the AWS console."
                    ),
                    "savings_inr": change_amt,
                    "confidence": "MEDIUM",
                    "signal_type": "spike",
                    "copyable_text": (
                        f"Action: Investigate {svc_name} spike — potential savings up to "
                        f"₹{change_amt:,.0f}/month (Medium confidence)"
                    ),
                }
            )

    # Sort by savings descending
    suggestions.sort(key=lambda s: s["savings_inr"], reverse=True)

    # ---- Priority 1: Usage-type-based suggestions (most accurate) --------
    if not suggestions and usage_breakdown:
        usage_sugg = _build_usage_type_suggestions(usage_breakdown, currency)
        if usage_sugg:
            return usage_sugg

    # ---- Fallback: service-based tips when no waste/spike signals available
    if not suggestions and records:
        suggestions = _build_service_suggestions(records, currency)

    return suggestions

"""
Usage-Type Breakdown Analyzer
Produces detailed category breakdowns from usage-type level billing data.

Outputs:
  - category_breakdown: EC2 Compute / EBS Storage / EBS Snapshots / Data Transfer / NAT Gateway / etc.
  - instance_breakdown: per-instance-type EC2 costs (c6a.2xlarge, r6a.4xlarge, etc.)
  - ebs_breakdown: gp3 volume / io2 volume / provisioned IOPS / throughput
  - data_transfer_breakdown: regional / cross-region / S3 egress / NAT
  - top_waste_category: service category with highest "avoidable" cost signal
  - waste_items: specific avoidable charges (idle IPs, excessive snapshots, etc.)
  - optimization_opportunities: pattern-matched optimizations with savings estimates
"""

from collections import defaultdict
import re

# Categories classified as "avoidable waste" with their waste percentages
_WASTE_RATES = {
    "EBS Snapshots":  0.70,   # most snapshots > 30d are unnecessary
    "NAT Gateway":    0.40,   # replaceable with VPC endpoints for AWS traffic
    "Data Transfer":  0.35,   # optimizable via CloudFront / same-region placement
    "EBS Storage":    0.20,   # unattached volumes + gp2→gp3 migration savings
    "CloudWatch":     0.35,   # log retention + unused metrics cleanup
    "VPC":            0.60,   # idle public IPs are pure waste
    "Load Balancer":  0.30,   # idle ALBs charge hourly even with zero traffic
    "KMS":            0.50,   # unused CMKs ($1/key/month)
    "EC2 Compute":    0.25,   # On-Demand → Reserved/Savings Plan
}

# Human-readable label overrides
_CATEGORY_LABELS = {
    "EC2 Compute":   "EC2 Compute (Instances)",
    "EBS Storage":   "EBS Storage (Volumes)",
    "EBS Snapshots": "EBS Snapshots (Backups)",
    "Data Transfer": "Data Transfer & Egress",
    "NAT Gateway":   "NAT Gateway",
    "Load Balancer": "Elastic Load Balancing",
}


def _extract_instance_type(usage_type: str) -> str:
    """Extract instance type from usage_type like 'BoxUsage:c6a.2xlarge'."""
    if "BoxUsage:" in usage_type:
        return usage_type.split("BoxUsage:")[-1].strip()
    if "ESInstance:" in usage_type:
        return "OpenSearch:" + usage_type.split("ESInstance:")[-1].strip()
    if "NodeUsage:cache." in usage_type:
        return "Cache:" + usage_type.split("NodeUsage:cache.")[-1].strip()
    return usage_type


def analyze(records: list[dict]) -> dict:
    """
    Args:
        records: normalised list from aws_parser.parse()["records"]
                 Expected to have format=="usage_type" for full detail,
                 but works (with reduced detail) for other formats too.
    Returns:
        {
          "available": bool,            # True only when usage_type data present
          "category_breakdown": [...],  # sorted by cost desc
          "instance_breakdown": [...],  # EC2 instance type breakdown
          "ebs_breakdown": [...],
          "data_transfer_breakdown": [...],
          "top_waste_category": {...} | None,
          "waste_items": [...],
          "total_avoidable_usd": float,
        }
    """
    # Check if usage_type data is present (non-empty usage_type fields)
    has_usage_types = any(r.get("usage_type", "") for r in records)

    if not has_usage_types:
        return {"available": False}

    # ---- Aggregate by category and usage_type ---------------------------
    category_totals: dict[str, float] = defaultdict(float)
    usage_type_totals: dict[str, float] = defaultdict(float)
    category_usage: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    total_cost = 0.0
    for r in records:
        svc = r["service"]
        ut = r.get("usage_type", "") or svc
        cost = r["cost"]
        category_totals[svc] += cost
        usage_type_totals[ut] += cost
        category_usage[svc][ut] += cost
        total_cost += cost

    # ---- Category breakdown (sorted by cost) ----------------------------
    category_breakdown = sorted(
        [
            {
                "category": svc,
                "label": _CATEGORY_LABELS.get(svc, svc),
                "total_usd": round(cost, 2),
                "pct_of_total": round(cost / total_cost * 100, 1) if total_cost else 0,
            }
            for svc, cost in category_totals.items()
        ],
        key=lambda x: x["total_usd"],
        reverse=True,
    )

    # ---- EC2 Instance type breakdown ------------------------------------
    instance_costs: dict[str, float] = defaultdict(float)
    for ut, cost in category_usage.get("EC2 Compute", {}).items():
        if "BoxUsage:" in ut:
            inst = _extract_instance_type(ut)
            instance_costs[inst] += cost
    instance_breakdown = sorted(
        [{"instance_type": k, "total_usd": round(v, 2)} for k, v in instance_costs.items()],
        key=lambda x: x["total_usd"],
        reverse=True,
    )[:8]

    # ---- EBS breakdown --------------------------------------------------
    ebs_groups: dict[str, float] = defaultdict(float)
    for ut, cost in {**category_usage.get("EBS Storage", {}),
                     **category_usage.get("EBS Snapshots", {})}.items():
        if "SnapshotUsage" in ut:
            ebs_groups["Snapshots"] += cost
        elif "VolumeUsage.gp3" in ut:
            ebs_groups["gp3 Volumes"] += cost
        elif "VolumeUsage.gp2" in ut:
            ebs_groups["gp2 Volumes (upgrade to gp3!)"] += cost
        elif "VolumeUsage.io2" in ut:
            ebs_groups["io2 Volumes"] += cost
        elif "VolumeP-IOPS" in ut:
            ebs_groups["Provisioned IOPS"] += cost
        elif "VolumeP-Throughput" in ut:
            ebs_groups["Provisioned Throughput"] += cost
        else:
            ebs_groups["Other EBS"] += cost
    ebs_breakdown = sorted(
        [{"label": k, "total_usd": round(v, 2)} for k, v in ebs_groups.items()],
        key=lambda x: x["total_usd"],
        reverse=True,
    )

    # ---- Data transfer breakdown ----------------------------------------
    dt_groups: dict[str, float] = defaultdict(float)
    for ut, cost in category_usage.get("Data Transfer", {}).items():
        if "regional" in ut.lower() or "Regional" in ut:
            dt_groups["Regional Transfer"] += cost
        elif "S3-Egress" in ut or "S3" in ut:
            dt_groups["S3 Egress"] += cost
        elif "AWS-Out-Bytes" in ut or "cross_region" in ut:
            dt_groups["Cross-Region Egress"] += cost
        elif "CloudFront" in ut:
            dt_groups["CloudFront Egress"] += cost
        else:
            dt_groups["Other Egress"] += cost
    # Also include NAT in data transfer context
    nat_cost = category_totals.get("NAT Gateway", 0)
    if nat_cost > 0:
        dt_groups["NAT Gateway"] += nat_cost
    dt_breakdown = sorted(
        [{"label": k, "total_usd": round(v, 2)} for k, v in dt_groups.items()],
        key=lambda x: x["total_usd"],
        reverse=True,
    )

    # ---- Waste items (specific avoidable charges) -----------------------
    waste_items = []

    # Idle public IPv4 addresses ($0.005/hr each = $3.6/month per idle IP)
    idle_ip_cost = sum(
        cost for ut, cost in category_usage.get("VPC", {}).items()
        if "IdleAddress" in ut
    )
    if idle_ip_cost > 0:
        waste_items.append({
            "type": "idle_public_ip",
            "title": "Idle Public IPv4 Addresses",
            "description": "You are being charged for public IPs not attached to any running instance. AWS charges $0.005/hr per idle IP.",
            "cost_usd": round(idle_ip_cost, 2),
            "avoidable_pct": 1.0,
            "confidence": "HIGH",
            "action": "Release unattached Elastic IPs in EC2 → Elastic IPs console",
        })

    # EBS Snapshots (assume 60% are older than 30 days and unnecessary)
    snapshot_cost = category_totals.get("EBS Snapshots", 0)
    if snapshot_cost > 5:
        waste_items.append({
            "type": "old_snapshots",
            "title": "Excessive EBS Snapshots",
            "description": f"${snapshot_cost:.2f}/month in EBS snapshot storage. Snapshots older than 30 days that aren't a recovery baseline are pure waste.",
            "cost_usd": round(snapshot_cost, 2),
            "avoidable_pct": 0.60,
            "confidence": "MEDIUM",
            "action": "Set a lifecycle policy: auto-delete snapshots older than 30 days (EC2 → Lifecycle Manager)",
        })

    # gp2 volumes (upgrade to gp3 is free performance + ~20% cheaper)
    gp2_cost = sum(
        cost for ut, cost in category_usage.get("EBS Storage", {}).items()
        if "VolumeUsage.gp2" in ut
    )
    if gp2_cost > 1:
        waste_items.append({
            "type": "gp2_volumes",
            "title": "gp2 EBS Volumes (Upgrade Available)",
            "description": f"${gp2_cost:.2f}/month on gp2 volumes. Migrating to gp3 is free in AWS and gives you 20% lower cost + 3x better baseline performance.",
            "cost_usd": round(gp2_cost, 2),
            "avoidable_pct": 0.20,
            "confidence": "HIGH",
            "action": "Modify each gp2 volume to gp3 type in EC2 → Volumes (no downtime required)",
        })

    # NAT Gateway data charges (often replaceable with VPC endpoints)
    nat_data_cost = sum(
        cost for ut, cost in category_usage.get("NAT Gateway", {}).items()
        if "nat_data" in ut or "Bytes" in ut
    )
    if nat_data_cost > 5:
        waste_items.append({
            "type": "nat_gateway_data",
            "title": "NAT Gateway Data Processing Costs",
            "description": f"${nat_data_cost:.2f}/month in NAT Gateway data charges. Traffic to AWS services (S3, DynamoDB, etc.) can bypass NAT Gateway via free VPC endpoints.",
            "cost_usd": round(nat_data_cost, 2),
            "avoidable_pct": 0.40,
            "confidence": "MEDIUM",
            "action": "Create VPC Gateway Endpoints for S3 and DynamoDB — free and eliminates NAT charges for that traffic",
        })

    # Regional data transfer (often preventable)
    regional_dt = sum(
        cost for ut, cost in category_usage.get("Data Transfer", {}).items()
        if "regional" in ut.lower() or "Regional" in ut
    )
    if regional_dt > 5:
        waste_items.append({
            "type": "regional_transfer",
            "title": "High Regional Data Transfer",
            "description": f"${regional_dt:.2f}/month in intra-region data transfer. This is often caused by services in different AZs communicating frequently.",
            "cost_usd": round(regional_dt, 2),
            "avoidable_pct": 0.30,
            "confidence": "MEDIUM",
            "action": "Colocate services in the same AZ, or use VPC endpoints. Review CloudWatch cross-AZ traffic metrics.",
        })

    # On-demand EC2 (no savings plan)
    compute_cost = category_totals.get("EC2 Compute", 0)
    if compute_cost > 50:
        waste_items.append({
            "type": "ondemand_ec2",
            "title": "EC2 On-Demand Pricing (No Savings Plan)",
            "description": f"${compute_cost:.2f}/month on EC2 compute at full On-Demand rates. Savings Plans or Reserved Instances typically save 30–45% for stable workloads.",
            "cost_usd": round(compute_cost, 2),
            "avoidable_pct": 0.30,
            "confidence": "MEDIUM",
            "action": "Purchase a 1-year Compute Savings Plan (no upfront) in AWS Cost Management → Savings Plans",
        })

    # Sort waste items by avoidable $ amount descending
    waste_items.sort(key=lambda x: x["cost_usd"] * x["avoidable_pct"], reverse=True)

    # ---- Total avoidable cost estimate ----------------------------------
    total_avoidable = sum(w["cost_usd"] * w["avoidable_pct"] for w in waste_items)

    # ---- Top waste category (by avoidable $ amount) ---------------------
    top_waste_category = None
    best_waste = 0.0
    for cat, cost in category_totals.items():
        rate = _WASTE_RATES.get(cat, 0)
        avoidable = cost * rate
        if avoidable > best_waste:
            best_waste = avoidable
            top_waste_category = {
                "category": cat,
                "label": _CATEGORY_LABELS.get(cat, cat),
                "total_usd": round(cost, 2),
                "avoidable_usd": round(avoidable, 2),
                "waste_rate_pct": round(rate * 100, 0),
            }

    return {
        "available": True,
        "category_breakdown": category_breakdown,
        "instance_breakdown": instance_breakdown,
        "ebs_breakdown": ebs_breakdown,
        "data_transfer_breakdown": dt_breakdown,
        "top_waste_category": top_waste_category,
        "waste_items": waste_items,
        "total_avoidable_usd": round(total_avoidable, 2),
    }

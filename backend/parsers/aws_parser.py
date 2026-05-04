"""
AWS Cost & Usage CSV Parser
Supports THREE billing CSV formats automatically:
  1. AWS Cost & Usage Report (CUR) — official export from AWS Billing console
     Detected by: lineItem/UnblendedCost column present
  2. Service-wide billing CSV — "Service" first column, service names as remaining cols
     e.g. downloaded from Cost Explorer grouped by Service, monthly granularity
  3. Usage-type wide billing CSV — "Usage type" first column, usage type codes as cols
     e.g. downloaded from Cost Explorer grouped by Usage Type, daily/monthly granularity

All formats are normalised to the same internal schema (date, service, usage_type, cost, region).
"""

import io
import re
import pandas as pd
from typing import Optional


# ---------------------------------------------------------------------------
# Simple-format column aliases  (used only in the non-CUR path)
# ---------------------------------------------------------------------------
DATE_ALIASES = [
    "Date", "date",
    "UsageStartDate", "BillingPeriodStartDate",
]
SERVICE_ALIASES = [
    "Service", "service",
    "ProductCode", "product/ProductCode",
]
REGION_ALIASES = [
    "Region", "region",
    "product/Region",
]
COST_ALIASES = [
    "Cost", "cost",
    "UnblendedCost", "BlendedCost", "TotalCost",
]
USAGE_TYPE_ALIASES = [
    "UsageType", "usage_type",
]


def _pick_column(df, aliases):
    """Return the first alias found in df.columns, else None."""
    for alias in aliases:
        if alias in df.columns:
            return alias
    return None


# ---------------------------------------------------------------------------
# Usage-type → service category mapping
# Priority: first matching rule wins (order matters)
# ---------------------------------------------------------------------------
_USAGE_TYPE_RULES = [
    # EC2 Compute
    ("BoxUsage:",               "EC2 Compute",    "compute"),
    ("EBSOptimized:",           "EC2 Compute",    "ebs_optimized_surcharge"),
    ("StoppedInstance",         "EC2 Compute",    "stopped_instance"),
    ("CPUCredits:",             "EC2 Compute",    "cpu_credits"),
    ("ECS-EC2",                 "EC2 Compute",    "ecs_ec2"),
    # EBS
    ("EBS:VolumeUsage",         "EBS Storage",    "ebs_volume"),
    ("EBS:VolumeP-IOPS",        "EBS Storage",    "ebs_iops"),
    ("EBS:VolumeP-Throughput",  "EBS Storage",    "ebs_throughput"),
    ("EBS:SnapshotUsage",       "EBS Snapshots",  "ebs_snapshot"),
    ("EBS:",                    "EBS Storage",    "ebs_other"),
    # NAT Gateway (before generic DataTransfer)
    ("NatGateway-Bytes",        "NAT Gateway",    "nat_data"),
    ("NatGateway-Hours",        "NAT Gateway",    "nat_hours"),
    ("NatGateway",              "NAT Gateway",    "nat_other"),
    # Data Transfer
    ("DataTransfer-Regional",   "Data Transfer",  "regional"),
    ("DataTransfer-Out",        "Data Transfer",  "egress"),
    ("DataTransfer-AZ",         "Data Transfer",  "inter_az"),
    ("DataTransfer-xAZ",        "Data Transfer",  "cross_az"),
    ("DataTransfer-In",         "Data Transfer",  "ingress"),
    ("DataTransfer",            "Data Transfer",  "other"),
    ("S3-Egress",               "Data Transfer",  "s3_egress"),
    ("AWS-Out-Bytes",           "Data Transfer",  "cross_region_egress"),
    ("AWS-In-Bytes",            "Data Transfer",  "cross_region_ingress"),
    ("DataProcessing-Bytes",    "Data Transfer",  "data_processing"),
    ("CloudFront-Out-Bytes",    "Data Transfer",  "cf_egress"),
    # Load Balancer
    ("LoadBalancerUsage",       "Load Balancer",  "hours"),
    ("LCUUsage",                "Load Balancer",  "lcu"),
    # CloudWatch
    ("CW:MetricMonitor",        "CloudWatch",     "metrics"),
    ("CW:AlarmMonitor",         "CloudWatch",     "alarms"),
    ("CW:Requests",             "CloudWatch",     "requests"),
    ("MonitoredEC2",            "CloudWatch",     "ec2_monitoring"),
    ("LogsAnalyzedBytes",       "CloudWatch",     "logs"),
    ("Application-Signals",     "CloudWatch",     "app_signals"),
    ("EventsAnalyzed",          "CloudWatch",     "events"),
    ("VendedLog",               "CloudWatch",     "vended_logs"),
    # ElastiCache
    ("NodeUsage:cache",         "ElastiCache",    "cache_node"),
    ("CachedData:",             "ElastiCache",    "cache_data"),
    ("ElastiCacheProcessing",   "ElastiCache",    "processing"),
    # Redshift
    ("Node:ra3",                "Redshift",       "node"),
    ("RMS:ra3",                 "Redshift",       "managed_storage"),
    # OpenSearch
    ("ESInstance:",             "OpenSearch",     "instance"),
    ("ES:GP3-Storage",          "OpenSearch",     "storage"),
    # S3
    ("TimedStorage-ByteHrs",    "S3",             "standard_storage"),
    ("TimedStorage-Z-ByteHrs",  "S3",             "ia_storage"),
    ("IATimedStorage",          "S3",             "ia_storage"),
    ("IA-TimedStorage",         "S3",             "ia_storage"),
    ("TimedPITRStorage",        "S3",             "pitr"),
    ("RequestV2-Tier0",         "S3",             "put_requests"),
    ("Requests-INT-Tier",       "S3",             "int_requests"),
    ("Requests-RBP",            "S3",             "rbp"),
    ("Inventory-Objects",       "S3",             "inventory"),
    ("Requests-Tier",           "S3",             "requests"),
    # DynamoDB
    ("WriteRequestUnits",       "DynamoDB",       "write"),
    ("ReadRequestUnits",        "DynamoDB",       "read"),
    ("ReadCapacityUnit",        "DynamoDB",       "rcu"),
    ("WriteCapacityUnit",       "DynamoDB",       "wcu"),
    ("Tables-Requests",         "DynamoDB",       "table_requests"),
    # VPC / Networking
    ("PublicIPv4:InUseAddress", "VPC",            "public_ip_inuse"),
    ("PublicIPv4:IdleAddress",  "VPC",            "public_ip_idle"),
    ("VpcEndpoint-Hours",       "VPC",            "endpoint_hours"),
    ("VpcEndpoint-Bytes",       "VPC",            "endpoint_bytes"),
    ("VpcPeering",              "VPC",            "peering"),
    ("ClientVPN",               "VPC",            "vpn"),
    ("VPN-Usage",               "VPC",            "vpn"),
    # WAF
    ("WebACLV2",                "WAF",            "webacl"),
    ("RuleV2",                  "WAF",            "rules"),
    # API Gateway
    ("ApiGatewayRequest",       "API Gateway",    "requests"),
    ("Gateway:Consumption",     "API Gateway",    "http_requests"),
    # KMS
    ("KMS-Requests",            "KMS",            "requests"),
    ("KMS-Keys",                "KMS",            "keys"),
    # Secrets Manager
    ("AWSSecretsManager-Secrets","Secrets Manager","secrets"),
    ("AWSSecretsManagerAPIRequest","Secrets Manager","api"),
    # CloudTrail
    ("PaidEventsRecorded",      "CloudTrail",     "paid_events"),
    ("FreeEventsRecorded",      "CloudTrail",     "free_events"),
    # Security Hub
    ("PaidComplianceCheck",     "Security Hub",   "compliance"),
    ("PaidFindingsIngestion",   "Security Hub",   "findings"),
    ("OtherProduct:Paid",       "Security Hub",   "findings"),
    ("SecurityHubProduct",      "Security Hub",   "hub"),
    # CloudFront
    ("Executions-CloudFrontFunctions", "CloudFront", "functions"),
    ("CloudFront-In-Bytes",     "CloudFront",     "ingress"),
    # Glue
    ("Catalog-Request",         "Glue",           "catalog_request"),
    ("Catalog-Storage",         "Glue",           "catalog_storage"),
    # SQS / SNS
    ("DeliveryAttempts-SQS",    "SQS",            "deliveries"),
    ("Event-64K-Chunks",        "SNS",            "events"),
    # Kiro
    ("KiroEnterprise",          "Kiro",           "enterprise"),
    # X-Ray
    ("XRay-Spans",              "X-Ray",          "spans"),
]


def _categorize_usage_type(col_name: str) -> tuple:
    """
    Map a usage type column (e.g. 'APS3-BoxUsage:c6a.2xlarge($)')
    to (service_category, sub_category, stripped_usage_code).
    """
    code = str(col_name).strip()
    if code.endswith("($)"):
        code = code[:-3].strip()

    # Strip region prefix like "APS3-", "USE1-", "Global-"
    m = re.match(r'^[A-Z][A-Z0-9]+\d-', code)
    if m:
        code_clean = code[m.end():]
    elif code.startswith("Global-"):
        code_clean = code[7:]
    else:
        code_clean = code

    for pattern, svc_cat, sub_cat in _USAGE_TYPE_RULES:
        if pattern in code or pattern in code_clean:
            return svc_cat, sub_cat, code_clean

    return "Other", "other", code_clean



# Mapping of verbose AWS product names -> short display names
_SERVICE_SHORT_NAMES = {
    "Amazon Elastic Compute Cloud": "Amazon EC2",
    "Amazon Elastic Compute Cloud - Compute": "Amazon EC2",
    "Amazon Elastic Block Store": "Amazon EBS",
    "Amazon Simple Storage Service": "Amazon S3",
    "Amazon Relational Database Service": "Amazon RDS",
    "Amazon Elastic Load Balancing": "Amazon ELB",
    "Amazon CloudFront": "CloudFront",
    "Amazon Route 53": "Route 53",
    "Amazon Virtual Private Cloud": "Amazon VPC",
    "Amazon Elastic Container Service": "Amazon ECS",
    "Amazon Elastic Kubernetes Service": "Amazon EKS",
    "Amazon Elastic Container Registry": "Amazon ECR",
    "Amazon ElastiCache": "ElastiCache",
    "Amazon DynamoDB": "DynamoDB",
    "Amazon Redshift": "Redshift",
    "Amazon OpenSearch Service": "OpenSearch",
    "Amazon Elasticsearch Service": "Elasticsearch",
    "Amazon Simple Queue Service": "Amazon SQS",
    "Amazon Simple Notification Service": "Amazon SNS",
    "Amazon Simple Email Service": "Amazon SES",
    "Amazon Kinesis": "Kinesis",
    "Amazon Kinesis Firehose": "Kinesis Firehose",
    "Amazon Kinesis Data Streams": "Kinesis Streams",
    "Amazon CloudWatch": "CloudWatch",
    "AWS CloudTrail": "CloudTrail",
    "AWS Lambda": "Lambda",
    "AWS Glue": "AWS Glue",
    "AWS Fargate": "Fargate",
    "AWS Secrets Manager": "Secrets Manager",
    "AWS Key Management Service": "AWS KMS",
    "AWS Certificate Manager": "ACM",
    "AWS Data Transfer": "Data Transfer",
    "AWSDataTransfer": "Data Transfer",
    "Amazon Glacier": "S3 Glacier",
    "Amazon S3 Glacier": "S3 Glacier",
    "Amazon Athena": "Athena",
    "Amazon SageMaker": "SageMaker",
    "Amazon Comprehend": "Comprehend",
    "Amazon Rekognition": "Rekognition",
    "Amazon Textract": "Textract",
    "Amazon Translate": "Translate",
    "Amazon API Gateway": "API Gateway",
    "Amazon Cognito": "Cognito",
    "Amazon Lightsail": "Lightsail",
    "Amazon WorkSpaces": "WorkSpaces",
    "AWS Direct Connect": "Direct Connect",
    "AWS Step Functions": "Step Functions",
    "AWS Backup": "AWS Backup",
    "AWS Config": "AWS Config",
    "AWS Systems Manager": "Systems Manager",
    "AWS CodeBuild": "CodeBuild",
    "AWS CodePipeline": "CodePipeline",
    "Amazon Elastic MapReduce": "Amazon EMR",
    "Amazon MSK": "Amazon MSK",
}


def _shorten_service(name):
    """Return a concise display name for a service, falling back to original."""
    if not name:
        return name
    return _SERVICE_SHORT_NAMES.get(name.strip(), name.strip())


def _friendly_region(region_code):
    """Translate AWS region codes to human-readable names."""
    mapping = {
        "ap-south-1":     "Asia Pacific (Mumbai)",
        "ap-south-2":     "Asia Pacific (Hyderabad)",
        "ap-southeast-1": "Asia Pacific (Singapore)",
        "ap-southeast-2": "Asia Pacific (Sydney)",
        "ap-northeast-1": "Asia Pacific (Tokyo)",
        "ap-northeast-2": "Asia Pacific (Seoul)",
        "us-east-1":      "US East (N. Virginia)",
        "us-east-2":      "US East (Ohio)",
        "us-west-1":      "US West (N. California)",
        "us-west-2":      "US West (Oregon)",
        "eu-west-1":      "Europe (Ireland)",
        "eu-west-2":      "Europe (London)",
        "eu-central-1":   "Europe (Frankfurt)",
        "ca-central-1":   "Canada (Central)",
        "sa-east-1":      "South America (Sao Paulo)",
        "global":         "Global",
        "":               "Unknown",
    }
    return mapping.get(str(region_code).strip(), region_code)


# ---------------------------------------------------------------------------
# Format-specific normalisers
# ---------------------------------------------------------------------------

def _normalize_cur(df):
    """
    Map official AWS CUR column names to internal names.
    Only called when lineItem/UnblendedCost is present.
    """
    df = df.copy()

    df["date"] = df["lineItem/UsageStartDate"]
    df["cost"] = df["lineItem/UnblendedCost"]

    # Service: prefer human-readable product name, fall back to product code
    if "product/ProductName" in df.columns:
        fallback = df["lineItem/ProductCode"] if "lineItem/ProductCode" in df.columns else "Unknown"
        df["service"] = df["product/ProductName"].fillna(fallback)
    elif "lineItem/ProductCode" in df.columns:
        df["service"] = df["lineItem/ProductCode"]
    else:
        df["service"] = "Unknown"

    # Region
    if "product/region" in df.columns:
        df["region"] = df["product/region"]
    elif "product/Region" in df.columns:
        df["region"] = df["product/Region"]
    else:
        df["region"] = "global"

    # Usage type (optional in CUR)
    df["usage_type"] = df["lineItem/UsageType"] if "lineItem/UsageType" in df.columns else ""

    return df


def _is_usage_type_wide(df) -> bool:
    """
    Detect the AWS Cost Explorer 'by Usage Type' wide-pivot export.
    Characteristics:
      - First column named "Usage type"
      - Columns contain usage type signatures (BoxUsage, EBS:, DataTransfer, NatGateway)
    """
    if df.empty or len(df.columns) < 4:
        return False
    if str(df.columns[0]).strip() != "Usage type":
        return False
    col_str = " ".join(str(c) for c in df.columns)
    return any(sig in col_str for sig in ("BoxUsage", "EBS:", "DataTransfer", "NatGateway"))


def _normalize_usage_type_wide(df):
    """
    Handle the AWS Cost Explorer 'by Usage Type' wide-pivot export.

    Shape coming in:
        Usage type | APS3-BoxUsage:c6a.2xlarge($) | APS3-EBS:VolumeUsage.gp3($) | ...
        Usage type total | 163.23 | 65.97 | ...
        2026-04-01 | | ... | 0.39
        2026-04-30 | 163.23 | 65.97 | ... | 649.99

    Transformed to normalised long format:
        date       | service       | usage_type            | cost  | region
        2026-04-30 | EC2 Compute   | BoxUsage:c6a.2xlarge  | 163.23| global
        2026-04-30 | EBS Storage   | EBS:VolumeUsage.gp3   | 65.97 | global
    """
    df = df.copy()
    first_col = df.columns[0]
    df = df.rename(columns={first_col: "date"})

    # Separate the aggregate summary row from actual date rows
    is_total_row = df["date"].astype(str).str.strip() == "Usage type total"
    date_rows = df[~is_total_row].copy()

    # Identify data columns (exclude date and "Total costs($)")
    skip_cols = {"date"}
    skip_cols.update(
        c for c in df.columns
        if str(c).strip().lower() in ("total costs($)", "total cost($)", "total($)")
    )
    usage_cols = [c for c in df.columns if c not in skip_cols]

    # Build per-column metadata
    col_info = {}  # col → (service_category, sub_category, usage_code)
    for col in usage_cols:
        svc, sub, code = _categorize_usage_type(col)
        col_info[col] = (svc, sub, code)

    # Melt wide → long (keep only rows with valid dates and non-zero costs)
    records = []
    for _, row in date_rows.iterrows():
        date_val = str(row["date"]).strip()
        try:
            pd.to_datetime(date_val)
        except Exception:
            continue

        for col in usage_cols:
            raw = row.get(col, "")
            cost = pd.to_numeric(str(raw).strip(), errors="coerce")
            if pd.isna(cost) or cost <= 0:
                continue
            svc_cat, sub_cat, usage_code = col_info[col]
            records.append({
                "date":       date_val,
                "service":    svc_cat,
                "usage_type": usage_code,
                "cost":       cost,
                "region":     "global",
            })

    if not records:
        raise ValueError("No valid usage-type records found after parsing.")

    return pd.DataFrame(records)


def _is_wide_billing(df) -> bool:
    """
    Return True when the CSV is the AWS Billing console 'monthly by service'
    wide-pivot export.  Characteristics:
      - First column is named "Service"
      - At least 3 other columns end with "($)"
    """
    if df.empty or len(df.columns) < 4:
        return False
    if str(df.columns[0]).strip() != "Service":
        return False
    dollar_cols = [c for c in df.columns if str(c).strip().endswith("($)")]
    return len(dollar_cols) >= 3


def _normalize_wide(df):
    """
    Handle the AWS Billing console 'by service / by month' wide-pivot export.

    Shape coming in:
        Service  | EC2-Instances($) | S3($) | ... | Total costs($)
        Service total | 286.82 | 8.11 | ...
        2025-11-01    |        |      | ...
        2026-04-01    | 286.82 | 8.11 | ...

    Transformed to normalised long format:
        date       | service        | cost  | region | usage_type
        2026-04-01 | EC2-Instances  | 286.82| global |
        2026-04-01 | S3             | 8.11  | global |
    """
    df = df.copy()

    # Rename the first column ("Service") to "date"
    first_col = df.columns[0]
    df = df.rename(columns={first_col: "date"})

    # Drop the "Service total" aggregate summary row
    df = df[df["date"].astype(str).str.strip() != "Service total"].copy()

    # Identify service columns — every column except "date" and "Total costs($)"
    skip_cols = {"date"}
    skip_cols.update(
        c for c in df.columns
        if str(c).strip().lower() in ("total costs($)", "total cost($)", "total($)")
    )
    service_cols = [c for c in df.columns if c not in skip_cols]

    # Build display-name map: "EC2-Instances($)" -> "EC2-Instances"
    col_to_service = {}
    for col in service_cols:
        name = str(col).strip()
        if name.endswith("($)"):
            name = name[:-3].strip()
        col_to_service[col] = name

    # Melt wide → long
    df = df.melt(
        id_vars=["date"],
        value_vars=service_cols,
        var_name="_svc_col",
        value_name="cost",
    )
    df["service"] = df["_svc_col"].map(col_to_service)
    df = df.drop(columns=["_svc_col"])

    # Coerce cost; drop rows that are empty or zero (months with no data)
    df["cost"] = df["cost"].replace("", float("nan"))
    df["cost"] = pd.to_numeric(df["cost"], errors="coerce")
    df = df[df["cost"].notna() & (df["cost"] > 0)].copy()

    df["region"] = "global"
    df["usage_type"] = ""

    return df


def _normalize_simple(df):
    """
    Map simplified / manual billing CSV columns to internal names.
    Called when CUR columns are absent.
    Raises ValueError if required columns cannot be found.
    """
    df = df.copy()

    date_col    = _pick_column(df, DATE_ALIASES)
    service_col = _pick_column(df, SERVICE_ALIASES)
    cost_col    = _pick_column(df, COST_ALIASES)
    region_col  = _pick_column(df, REGION_ALIASES)
    usage_col   = _pick_column(df, USAGE_TYPE_ALIASES)

    missing = []
    if not date_col:    missing.append("date")
    if not service_col: missing.append("service")
    if not cost_col:    missing.append("cost")
    if missing:
        raise ValueError(
            f"CSV is missing required columns: {missing}. "
            f"Found columns: {list(df.columns)}"
        )

    df["date"]       = df[date_col]
    df["cost"]       = df[cost_col]
    df["service"]    = df[service_col]
    df["region"]     = df[region_col].fillna("global") if region_col else "global"
    df["usage_type"] = df[usage_col].fillna("")        if usage_col  else ""

    return df


# ---------------------------------------------------------------------------
# Shared post-normalisation cleanup (runs for BOTH formats)
# ---------------------------------------------------------------------------

def _clean(df, errors):
    """
    Enforce correct dtypes, drop unparseable rows, ignore credits.
    Mutates the errors list with any warnings generated.
    """
    # Convert date to Python date object (handles ISO strings AND timestamps)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date

    df["service"]    = df["service"].fillna("Unknown").astype(str).str.strip().apply(_shorten_service)
    df["region"]     = df["region"].fillna("global").astype(str).str.strip()
    df["cost"]       = pd.to_numeric(df["cost"], errors="coerce").fillna(0.0)
    df["usage_type"] = df["usage_type"].fillna("").astype(str).str.strip()

    original_len = len(df)
    df = df.dropna(subset=["date"])
    df = df[df["cost"] >= 0]   # ignore credit / refund lines for MVP
    dropped = original_len - len(df)
    if dropped > 0:
        errors.append(f"{dropped} row(s) skipped (unparseable date or negative cost).")

    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(file_bytes):
    """
    Parse an AWS billing CSV (CUR or simplified) and return a dict:
      {
        "records":    [ {date, service, region, region_friendly, cost, usage_type}, ... ],
        "days_count": int,
        "date_range": { "start": "YYYY-MM-DD", "end": "YYYY-MM-DD" },
        "services":   [ str, ... ],
        "regions":    [ str, ... ],
        "errors":     [ str, ... ],
        "format":     "CUR" | "simple",
      }
    Raises ValueError on unrecoverable parse errors.
    """
    errors = []

    # ---- Load raw CSV -------------------------------------------------------
    try:
        df = pd.read_csv(io.BytesIO(file_bytes), low_memory=False)
    except Exception as exc:
        raise ValueError(f"Could not read CSV: {exc}")

    if df.empty:
        raise ValueError("The uploaded CSV is empty.")

    # ---- Detect format and normalise ----------------------------------------
    if "lineItem/UnblendedCost" in df.columns:
        fmt = "CUR"
        df = _normalize_cur(df)
    elif _is_usage_type_wide(df):
        fmt = "usage_type"
        df = _normalize_usage_type_wide(df)
    elif _is_wide_billing(df):
        fmt = "wide"
        df = _normalize_wide(df)
    else:
        fmt = "simple"
        df = _normalize_simple(df)   # raises ValueError if required cols missing

    # ---- Shared cleanup (date conversion, type coercion, drop bad rows) ------
    df = _clean(df, errors)

    if df.empty:
        raise ValueError("No valid billing rows found after parsing.")

    # ---- Build final records list -------------------------------------------
    records = [
        {
            "date":            row["date"].isoformat(),
            "service":         row["service"],
            "region":          row["region"],
            "region_friendly": _friendly_region(row["region"]),
            "cost":            round(float(row["cost"]), 4),
            "usage_type":      row["usage_type"],
        }
        for _, row in df[["date", "service", "region", "cost", "usage_type"]].iterrows()
    ]

    dates    = sorted(set(r["date"]    for r in records))
    services = sorted(set(r["service"] for r in records))
    regions  = sorted(set(r["region"]  for r in records))

    # ---- Currency detection ---------------------------------------------
    if fmt in ("wide", "usage_type"):
        currency = "USD"  # wide billing formats always use ($) column names
    elif fmt == "CUR" and "lineItem/CurrencyCode" in df.columns:
        codes = df["lineItem/CurrencyCode"].dropna().unique().tolist()
        currency = str(codes[0]) if codes else "USD"
    else:
        currency = "USD"  # default assumption for all other formats

    return {
        "records":    records,
        "days_count": len(dates),
        "date_range": {"start": dates[0], "end": dates[-1]},
        "services":   services,
        "regions":    regions,
        "errors":     errors,
        "format":     fmt,
        "currency":   currency,
    }
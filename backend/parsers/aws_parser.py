"""
AWS Cost & Usage CSV Parser
Supports TWO formats automatically:
  1. AWS Cost & Usage Report (CUR) - official export from AWS Billing console
     Detected by: lineItem/UnblendedCost column present
     Key columns: lineItem/UsageStartDate, lineItem/UnblendedCost,
                  product/ProductName, product/region, lineItem/UsageType
  2. Simplified billing CSV - manual exports, sample files, third-party tools
     Key columns: Date/date, Cost/cost, Service/service, Region/region

Both formats are normalised to the same internal schema, then continue
through the same analysis pipeline unchanged.
"""

import io
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

    return {
        "records":    records,
        "days_count": len(dates),
        "date_range": {"start": dates[0], "end": dates[-1]},
        "services":   services,
        "regions":    regions,
        "errors":     errors,
        "format":     fmt,
    }
"""Shared AWS operations used by the data pipeline and conversational agent.

Read this module after looking at the architecture in README.md. It contains
small functions for configuration, JSON objects in S3, and Athena execution.
Keeping these operations here prevents the ingestion job and agent from having
different rules for credentials, query timeouts, or pipeline readiness.

Nothing connects to AWS when this module is imported. A connection is created
only when a caller explicitly invokes one of the functions below.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


# This file lives in shared/, one level below the project root. The packaged
# AgentCore artifact preserves that layout, so the same path works in AWS.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def utc_now():
    """Return an ISO timestamp in UTC for processing and status records.

    These timestamps describe when our pipeline runs. They must not replace
    date_received, which is the business date supplied by the original source.
    """
    return datetime.now(timezone.utc).isoformat()


def load_config(s3_uri=None):
    """Read configuration from S3 for Glue, or from a local file otherwise.

    Args:
        s3_uri: Optional URI such as s3://bucket/code/config.json. Glue supplies
            this argument because its script does not have our local config.

    Returns:
        A dictionary containing resource names, model IDs, and runtime settings.
        The bucket's ACCOUNT_ID token is resolved using the current AWS identity.

    Raises:
        ValueError: A local file or a required setting is missing.
        ClientError: S3 could not be read; access errors must remain visible.
    """
    if s3_uri:
        # The first slash separates the bucket from the complete object key.
        # This initial S3 client uses the job's AWS role and default AWS region.
        bucket, key = s3_uri.removeprefix("s3://").split("/", 1)
        response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        config = json.loads(response["Body"].read())
    else:
        # COPILOT_CONFIG is useful for a separate local configuration. Ordinary
        # classroom execution uses config/config.json without any environment flag.
        default_path = PROJECT_ROOT / "config" / "config.json"
        config_path = Path(os.environ.get("COPILOT_CONFIG", default_path))

        if not config_path.exists():
            raise ValueError(
                "The project requires config/config.json. "
                "Use the supplied file from this project."
            )

        config = json.loads(config_path.read_text())

    # Names are already chosen in the supplied file. AWS-generated IDs are
    # looked up only when their service is needed; see resource_lookup.py.
    # Configuration contains no credentials. boto3 uses the existing local
    # credentials or the service role attached to Glue / AgentCore.
    required_fields = (
        "region",
        "bucket",
        "knowledge_base_name",
        "data_source_name",
        "agent_runtime_name",
    )

    for field in required_fields:
        value = config.get(field)

        if not value:
            raise ValueError("Complete the configuration value: " + field)

    # All learners use the same package, including the prepared AgentCore ZIP.
    # STS tells us the account running this code, so each account gets its own
    # predictable bucket name without editing or rebuilding the ZIP. Resolve
    # only the in-memory copy; never rewrite local or S3 configuration here.
    # Local credentials and deployed roles must belong to the project account.
    if "ACCOUNT_ID" in config["bucket"]:
        account_id = client("sts", config).get_caller_identity()["Account"]
        config["bucket"] = config["bucket"].replace("ACCOUNT_ID", account_id)

    return config


def client(service, config):
    """Create a boto3 client with the same region and retry policy everywhere.

    An AgentCore request contains a complete conversation turn, potentially with
    several model/tool calls. Give that outer request a longer read timeout than
    an individual request to S3, Athena, or Bedrock.
    """
    read_timeout = 600 if service == "bedrock-agentcore" else 120

    sdk_settings = Config(
        retries={"max_attempts": 4, "mode": "standard"},
        connect_timeout=10,
        read_timeout=read_timeout,
    )

    return boto3.client(
        service,
        region_name=config["region"],
        config=sdk_settings,
    )


def json_bytes(value):
    """Serialize a JSON-compatible value into deterministic UTF-8 bytes.

    Sorting keys means a different dictionary key order does not change a
    checksum. Keeping Unicode preserves the original complaint text.
    """
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return serialized.encode("utf-8")


def digest(value):
    """Return a stable SHA-256 checksum for a record, article, or input batch.

    Ingestion compares these checksums to skip unchanged documents and reject
    attempts to edit a batch that has already been committed.
    """
    return hashlib.sha256(json_bytes(value)).hexdigest()


def read_json(s3, bucket, key, default=None):
    """Read one JSON object from S3, using a default only when it is absent.

    The default permits an empty checkpoint on the first ingestion run. An
    AccessDenied error must not be treated as an empty checkpoint, because doing
    so could make an existing dataset appear to be a new installation.
    """
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read())
    except ClientError as error:
        error_code = error.response["Error"]["Code"]

        if error_code in ("NoSuchKey", "404"):
            return default

        raise


def write_json(s3, bucket, key, value):
    """Replace one S3 object with UTF-8 JSON and the correct content type.

    S3 replaces this single key as an object. This is not a transaction across
    other S3 keys, Athena, and OpenSearch; ingestion handles readiness separately.
    """
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json_bytes(value),
        ContentType="application/json",
    )


def run_sql(config, sql):
    """Execute an Athena query and return its rows plus trace information.

    The agent validates generated SQL before calling this function. Ingestion
    calls it with a fixed count query for reconciliation. Both callers use the
    configured workgroup, so its scan limit and output settings also apply.

    Returns:
        A dictionary with sql, query_id, rows, and bytes_scanned. Athena values
        arrive as strings; an SQL NULL is represented by None.
    """
    athena = client("athena", config)

    # 1. Submit the query. Disable result reuse so a new daily batch cannot be
    # hidden by an old cached count. Query outputs stay in a dedicated prefix.
    response = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": config["database"]},
        WorkGroup=config["workgroup"],
        ResultConfiguration={
            "OutputLocation": "s3://" + config["bucket"] + "/athena-results/",
        },
        ResultReuseConfiguration={
            "ResultReuseByAgeConfiguration": {"Enabled": False},
        },
    )
    query_id = response["QueryExecutionId"]

    # 2. Athena is asynchronous. Poll its execution status until it succeeds,
    # fails, or reaches the deadline. A timeout also cancels the query in AWS.
    deadline = time.monotonic() + config.get("athena_timeout_seconds", 60)

    while time.monotonic() < deadline:
        response = athena.get_query_execution(QueryExecutionId=query_id)
        execution = response["QueryExecution"]
        state = execution["Status"]["State"]

        if state == "SUCCEEDED":
            break

        if state in ("FAILED", "CANCELLED"):
            reason = execution["Status"].get("StateChangeReason", state)
            raise RuntimeError(reason)

        time.sleep(1)
    else:
        athena.stop_query_execution(QueryExecutionId=query_id)
        raise TimeoutError("Athena query exceeded its timeout: " + query_id)

    # 3. Generated queries are capped at 100 rows. Request 101 because Athena's
    # first returned row is the header. Metadata supplies the actual column names.
    page = athena.get_query_results(
        QueryExecutionId=query_id,
        MaxResults=101,
    )
    column_info = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
    column_names = [column["Name"] for column in column_info]

    rows = []

    for result_row in page["ResultSet"]["Rows"][1:]:
        values = [cell.get("VarCharValue") for cell in result_row["Data"]]
        rows.append(dict(zip(column_names, values)))

    # The query ID lets a student inspect the exact execution in the console.
    return {
        "sql": sql,
        "query_id": query_id,
        "rows": rows,
        "bytes_scanned": execution.get("Statistics", {}).get("DataScannedInBytes", 0),
    }


def pipeline_status(config):
    """Return the committed dataset status, refusing an incomplete refresh.

    The agent calls this before and after a turn. A READY status plus an
    unchanged epoch means no pipeline refresh was detected during that turn.
    """
    status = read_json(
        client("s3", config),
        config["bucket"],
        "state/pipeline.json",
        {},
    )

    if status.get("status") != "READY":
        current_state = status.get("status", "NOT_STARTED")
        raise RuntimeError("Dataset is not ready. Pipeline status: " + current_state)

    return status

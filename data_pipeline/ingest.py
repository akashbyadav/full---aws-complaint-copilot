"""Process one daily batch with an AWS Glue Python shell job.

The flow is deliberately sequential:
    select input -> validate -> publish UPDATING -> write Parquet and documents
    -> synchronize the knowledge base -> verify visibility -> publish READY.

Run only one instance of this job at a time. Its S3 checkpoint holds the current
records for our small teaching cohort; it is not a distributed state store.

Glue receives --CONFIG_S3_URI and optionally --BATCH_KEY. The prepared shared/
library is a Python wheel supplied through Glue's --extra-py-files argument.
"""

import argparse
import copy
import io
import json
import re
import time
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq

from shared.aws_helpers import (
    client,
    digest,
    load_config,
    read_json,
    run_sql,
    utc_now,
    write_json,
)
from shared.resource_lookup import data_source_id, knowledge_base_id


# Parquet files must have identical types across every batch, including an empty
# guidance-only batch. Explicit types prevent an all-empty column being inferred
# differently from the same column in a later file.
SCHEMA = pa.schema(
    [
        ("complaint_id", pa.string()),
        ("date_received", pa.string()),
        ("company", pa.string()),
        ("product", pa.string()),
        ("issue", pa.string()),
        ("state", pa.string()),
        ("company_response", pa.string()),
        ("timely_response", pa.string()),
        ("has_narrative", pa.bool_()),
        ("record_version", pa.int64()),
        ("is_deleted", pa.bool_()),
        ("is_demo_fixture", pa.bool_()),
        ("batch_id", pa.string()),
    ]
)

# These fields form the complete incoming record contract. A correction must
# supply the complete record, not just the field that changed.
STRING_FIELDS = (
    "complaint_id",
    "date_received",
    "company",
    "product",
    "issue",
    "state",
    "company_response",
    "timely_response",
    "narrative",
    "source_url",
)


def validate_batch(batch):
    """Validate the entire input before either query destination is changed.

    The caller quarantines a rejected batch with its error message. We do not
    silently drop invalid rows: doing so would hide data quality problems.

    An empty records array is valid for a guidance-only refresh. A missing
    complaint in a delta is not a deletion; deletion needs an explicit tombstone.
    """
    batch_id = batch.get("batch_id", "")

    if not re.fullmatch(r"batch-\d{3,}", batch_id):
        raise ValueError("batch_id must look like batch-001.")

    sequence = batch.get("sequence")
    batch_number = int(batch_id.split("-")[1])

    if type(sequence) is not int or sequence < 1 or sequence != batch_number:
        raise ValueError("sequence must match the numeric batch ID.")

    if not isinstance(batch.get("records"), list):
        raise ValueError(
            "records must be a JSON array; "
            "an empty array allows a guidance-only refresh."
        )

    seen_ids = set()

    for record in batch["records"]:
        # Empty narrative text is allowed, but the field itself must exist.
        # Such records participate in SQL without creating narrative vectors.
        for field in STRING_FIELDS:
            if not isinstance(record.get(field), str):
                raise ValueError("Every record must contain all string fields.")

        complaint_id = record["complaint_id"]

        if not complaint_id.isdigit() or complaint_id in seen_ids:
            raise ValueError(
                "Complaint IDs must be numeric strings and unique within a batch."
            )

        seen_ids.add(complaint_id)
        date.fromisoformat(record["date_received"])

        if not record["company"] or not record["product"]:
            raise ValueError("Company and product are required.")

        if (
            type(record.get("record_version")) is not int
            or record["record_version"] != sequence
        ):
            raise ValueError("Replay record_version must equal the batch sequence.")

        # Python treats bool as a subclass of int. Exact type checks distinguish
        # JSON true/false from accidental numeric 1/0 values in this contract.
        for field in ("is_deleted", "is_demo_fixture"):
            if type(record.get(field)) is not bool:
                raise ValueError(field + " must be a JSON boolean.")


def choose_batch_key(s3, bucket, checkpoint, old_status, requested_key=None):
    """Select a recovery batch, an explicit replay, or the next queued batch.

    A partially written batch must finish before processing another batch. For
    ordinary scheduled runs, list raw input objects and pick the first numeric
    sequence that is not already recorded in processed_batches.

    Returns:
        An S3 object key, or None when no unprocessed input is available.
    """
    interrupted = old_status.get("status") in ("UPDATING", "FAILED")
    interrupted_key = old_status.get("batch_key")

    if requested_key:
        if interrupted and interrupted_key != requested_key:
            raise ValueError(
                "Recover the interrupted batch first: " + str(interrupted_key)
            )

        return requested_key

    if interrupted and interrupted_key:
        return interrupted_key

    # Paginate instead of assuming the S3 listing fits into a single response.
    # Only correctly named batch objects participate in the input queue.
    keys = []
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix="raw/batches/"):
        for item in page.get("Contents", []):
            if re.search(r"/batch-\d{3,}\.json$", item["Key"]):
                keys.append(item["Key"])

    keys.sort(key=lambda key: int(key.rsplit("-", 1)[1][:-5]))

    for key in keys:
        batch_id = key.rsplit("/", 1)[1][:-5]

        if batch_id not in checkpoint["processed_batches"]:
            return key

    return None


def write_parquet_batch(s3, bucket, batch):
    """Write one deterministic Parquet object containing this batch's versions.

    A retry replaces the same object rather than appending duplicate rows.
    Historical versions remain in other batch objects; the Athena view chooses
    the latest active version for each complaint ID.
    """
    parquet_rows = []

    for record in batch["records"]:
        # Narrative text stays in the raw input/checkpoint and knowledge path.
        # SQL only needs the boolean indicating whether narrative text exists.
        output = {field.name: record.get(field.name) for field in SCHEMA}
        output["batch_id"] = batch["batch_id"]
        output["has_narrative"] = bool(record["narrative"])
        parquet_rows.append(output)

    table = pa.Table.from_pylist(parquet_rows, schema=SCHEMA)
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")

    s3.put_object(
        Bucket=bucket,
        Key="curated/complaint_versions/" + batch["batch_id"] + ".parquet",
        Body=buffer.getvalue(),
        ContentType="application/octet-stream",
    )


def put_document(s3, bucket, key, text, metadata):
    """Write a source document and its Bedrock metadata sidecar at stable keys.

    The metadata file must be named <document-key>.metadata.json. Bedrock applies
    these attributes to generated chunks. We manage logical source documents;
    the knowledge base manages chunk IDs, embeddings and OpenSearch writes.
    """
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )

    write_json(
        s3,
        bucket,
        key + ".metadata.json",
        {"metadataAttributes": metadata},
    )


def update_documents(
    config,
    s3,
    records,
    previous,
    guidance,
    old_guidance_hashes,
):
    """Write changed documents and return the changes to verify after sync.

    Args:
        records: Current complaint records after merging the incoming batch.
        previous: Complaint records from the last committed checkpoint.
        guidance: Complete current catalog of saved guidance articles.
        old_guidance_hashes: Article checksums from the committed checkpoint.

    Returns:
        A pair: visibility checks for changed logical IDs, and new article hashes.
        Checks specify an expected record version, article hash, or absence.
    """
    bucket = config["bucket"]
    changed_documents = []

    # 1. Complaint updates are deltas. Only changed records need S3 writes.
    # Rewriting an unchanged document would trigger avoidable ingestion work.
    for complaint_id, record in records.items():
        old_record = previous.get(complaint_id)

        if digest(record) == digest(old_record):
            continue

        document_key = "knowledge/complaints/" + complaint_id + ".txt"

        if record["is_deleted"] or not record["narrative"]:
            # A new SQL-only row never had a narrative document. It therefore
            # does not need deletion requests or retrieval absence probes.
            if (
                not old_record
                or old_record["is_deleted"]
                or not old_record["narrative"]
            ):
                continue

            for key in (document_key, document_key + ".metadata.json"):
                s3.delete_object(Bucket=bucket, Key=key)

            changed_documents.append(
                {
                    "document_id": complaint_id,
                    "absent": True,
                }
            )
            continue

        metadata = {
            "document_id": complaint_id,
            "source_type": "complaint",
            "company": record["company"],
            "product": record["product"],
            "date_received": record["date_received"],
            "date_received_number": int(record["date_received"].replace("-", "")),
            "record_version": record["record_version"],
            "source_url": record["source_url"],
            "is_demo_fixture": record["is_demo_fixture"],
        }

        # Labels help keyword retrieval. Keep the narrative itself intact,
        # including the source's redactions and any explicit classroom markers.
        document_text = (
            "Complaint ID: "
            + complaint_id
            + "\nCompany: "
            + record["company"]
            + "\nProduct: "
            + record["product"]
            + "\nIssue: "
            + record["issue"]
            + "\nDate received: "
            + record["date_received"]
            + "\n\n"
            + record["narrative"]
        )

        put_document(s3, bucket, document_key, document_text, metadata)

        changed_documents.append(
            {
                "document_id": complaint_id,
                "version": record["record_version"],
            }
        )

    # 2. Guidance is a complete catalog, not a complaint-style delta. Compare
    # article hashes to update only additions and content/metadata corrections.
    new_guidance_hashes = {}

    for article in guidance:
        document_id = article["document_id"]
        article_hash = digest(article)
        new_guidance_hashes[document_id] = article_hash

        if old_guidance_hashes.get(document_id) == article_hash:
            continue

        put_document(
            s3,
            bucket,
            "knowledge/guidance/" + document_id + ".txt",
            article["title"] + "\n\n" + article["text"],
            {
                "document_id": document_id,
                "source_type": "guidance",
                "source_url": article["url"],
                "title": article["title"],
                "content_hash": article_hash,
            },
        )

        changed_documents.append(
            {
                "document_id": document_id,
                "hash": article_hash,
            }
        )

    # An article removed from a full catalog is an explicit catalog deletion.
    removed_ids = set(old_guidance_hashes) - set(new_guidance_hashes)

    for document_id in removed_ids:
        document_key = "knowledge/guidance/" + document_id + ".txt"

        for key in (document_key, document_key + ".metadata.json"):
            s3.delete_object(Bucket=bucket, Key=key)

        changed_documents.append(
            {
                "document_id": document_id,
                "absent": True,
            }
        )

    return changed_documents, new_guidance_hashes


def synchronize(config):
    """Wait for a complete knowledge-base ingestion job and return its ID.

    Starting ingestion is asynchronous. Success requires COMPLETE with zero
    document failures, followed by separate query-visibility checks.

    If an earlier Glue run left a sync running, wait for it first and then start
    a new incremental pass. The old sync may have captured its source snapshot
    before the retry rewrote the documents.
    """
    bedrock = client("bedrock-agent", config)
    parameters = {
        "knowledgeBaseId": knowledge_base_id(config),
        "dataSourceId": data_source_id(config),
    }

    response = bedrock.list_ingestion_jobs(
        **parameters,
        maxResults=10,
        sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
    )

    running_job = None

    for job in response.get("ingestionJobSummaries", []):
        if job["status"] in ("STARTING", "IN_PROGRESS"):
            running_job = job
            break

    if running_job:
        job_id = running_job["ingestionJobId"]
    else:
        response = bedrock.start_ingestion_job(**parameters)
        job_id = response["ingestionJob"]["ingestionJobId"]

    deadline = time.monotonic() + config.get("ingestion_timeout_seconds", 1800)

    while time.monotonic() < deadline:
        response = bedrock.get_ingestion_job(
            **parameters,
            ingestionJobId=job_id,
        )
        job = response["ingestionJob"]

        if job["status"] == "COMPLETE":
            failed_count = job.get("statistics", {}).get(
                "numberOfDocumentsFailed",
                0,
            )

            if failed_count:
                raise RuntimeError(
                    "Knowledge-base sync completed with failed documents: "
                    + json.dumps(job, default=str)
                )

            if running_job:
                # Drain the interrupted run, then include all writes from this
                # attempt in a fresh incremental scan of the source prefix.
                running_job = None
                response = bedrock.start_ingestion_job(**parameters)
                job_id = response["ingestionJob"]["ingestionJobId"]
                continue

            return job_id

        if job["status"] in ("FAILED", "STOPPED"):
            raise RuntimeError(
                "Knowledge-base sync failed: "
                + json.dumps(job.get("failureReasons", []))
            )

        time.sleep(10)

    raise TimeoutError(
        "Knowledge-base ingestion did not finish within its time budget."
    )


def wait_for_visibility(config, changes):
    """Check that changed logical documents are queryable after synchronization.

    A successful ingestion status can precede retrieval visibility. Probe each
    changed ID using a metadata filter, then verify its expected version/hash.
    A deletion must return no passage on two probes.

    These are readiness checks for this small cohort, not a guarantee of
    transaction isolation across every OpenSearch shard or every document chunk.
    """
    runtime = client("bedrock-agent-runtime", config)
    deadline = time.monotonic() + config.get("visibility_timeout_seconds", 600)
    pending = list(changes)
    absence_counts = {}

    while pending and time.monotonic() < deadline:
        remaining = []

        for change in pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Retrieval visibility checks exceeded their time budget."
                )

            response = runtime.retrieve(
                knowledgeBaseId=knowledge_base_id(config),
                retrievalQuery={"text": change["document_id"]},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {
                        "numberOfResults": 1,
                        "overrideSearchType": "SEMANTIC",
                        "filter": {
                            "equals": {
                                "key": "document_id",
                                "value": change["document_id"],
                            },
                        },
                    },
                },
            )

            results = response.get("retrievalResults", [])
            metadata = results[0].get("metadata", {}) if results else {}

            if change.get("absent"):
                if results:
                    absence_counts[change["document_id"]] = 0
                else:
                    previous_count = absence_counts.get(change["document_id"], 0)
                    absence_counts[change["document_id"]] = previous_count + 1

                visible = absence_counts[change["document_id"]] >= 2

            elif "version" in change:
                visible = metadata.get("record_version") == change["version"]

            else:
                visible = metadata.get("content_hash") == change["hash"]

            if not visible:
                remaining.append(change)

        # Already confirmed IDs leave the queue. Only unconfirmed changes are
        # queried again, keeping the waiting loop bounded and understandable.
        pending = remaining

        if pending:
            time.sleep(10)

    if pending:
        raise TimeoutError(
            "Retrieval visibility is incomplete for "
            + str(len(pending))
            + " documents."
        )


def ingest(config, batch_key=None):
    """Run the daily workflow and return its final pipeline-status dictionary.

    The checkpoint is committed only after both destinations pass the checks.
    If writes fail midway, the old checkpoint remains available for recovery and
    pipeline.json records FAILED. Rerunning the same batch uses the same keys.
    """
    s3 = client("s3", config)
    bucket = config["bucket"]

    # 1. Load committed state. Defaults are used only for a brand-new project.
    checkpoint = read_json(
        s3,
        bucket,
        "state/checkpoint.json",
        {
            "sequence": 0,
            "records": {},
            "processed_batches": {},
            "guidance_hashes": {},
        },
    )
    old_status = read_json(s3, bucket, "state/pipeline.json", {})

    batch_key = choose_batch_key(
        s3,
        bucket,
        checkpoint,
        old_status,
        requested_key=batch_key,
    )

    if batch_key is None:
        print("No new batch. Nothing changed.")
        return old_status

    batch = read_json(s3, bucket, batch_key)

    if batch is None:
        raise ValueError("Input batch does not exist: " + batch_key)

    batch_hash = digest(batch)
    batch_id = batch.get("batch_id", "invalid")

    # 2. Validate identity, checksum and order before touching either destination.
    # Rejecting input here leaves the previously committed dataset available.
    try:
        validate_batch(batch)

        if batch_key != "raw/batches/" + batch_id + ".json":
            raise ValueError("The input object key must match its batch_id.")

        previous_hash = checkpoint["processed_batches"].get(batch_id)

        if previous_hash and previous_hash != batch_hash:
            raise ValueError(
                "A processed batch is immutable. Submit corrections in a NEW batch."
            )

        if previous_hash and old_status.get("status") == "READY":
            print(
                (
                    "This exact batch is already processed; counts and vectors "
                    "are unchanged."
                )
            )
            return old_status

        if not previous_hash and batch["sequence"] != checkpoint["sequence"] + 1:
            raise ValueError(
                "Process batches in sequence. Next sequence is "
                + str(checkpoint["sequence"] + 1)
            )

    except Exception as error:
        write_json(
            s3,
            bucket,
            "quarantine/" + batch_hash + ".json",
            {"error": str(error), "batch": batch},
        )
        raise

    # 3. Change the epoch BEFORE writing queryable data. An in-flight agent turn
    # will detect this change and refuse to publish a potentially mixed answer.
    status = {
        "status": "UPDATING",
        "epoch": utc_now(),
        "batch_id": batch_id,
        "batch_key": batch_key,
        "last_success_at": old_status.get("last_success_at"),
    }
    write_json(s3, bucket, "state/pipeline.json", status)

    try:
        # 4. Merge complete record versions into a separate in-memory copy.
        # The old checkpoint is still intact if any following operation fails.
        current_records = copy.deepcopy(checkpoint["records"])

        for record in batch["records"]:
            existing = current_records.get(record["complaint_id"])

            if existing and record["record_version"] < existing["record_version"]:
                continue

            current_records[record["complaint_id"]] = record

        # 5. Prepare the two destinations. Stable output keys make retries replace
        # the same data instead of appending duplicate versions or documents.
        write_parquet_batch(s3, bucket, batch)
        guidance = read_json(s3, bucket, "raw/guidance/articles.json")

        if not guidance:
            raise ValueError("Upload data/guidance/articles.json before ingestion.")

        changes, guidance_hashes = update_documents(
            config,
            s3,
            current_records,
            checkpoint["records"],
            guidance,
            checkpoint["guidance_hashes"],
        )

        # 6. Wait for vector ingestion AND retrieval visibility. A response from
        # StartIngestionJob alone does not mean the new documents can be searched.
        ingestion_job_id = synchronize(config)
        wait_for_visibility(config, changes)

        # 7. Reconcile Athena against the in-memory merged records. SQL rows and
        # vector chunks intentionally have different counts; compare like to like.
        active_records = [
            record for record in current_records.values() if not record["is_deleted"]
        ]
        expected_count = len(active_records)

        result = run_sql(
            config,
            "SELECT COUNT(*) AS complaint_count FROM complaints_current",
        )
        actual_count = int(result["rows"][0]["complaint_count"])

        if actual_count != expected_count:
            raise RuntimeError(
                "Reconciliation failed: expected %s rows, Athena returned %s."
                % (expected_count, actual_count)
            )

        # 8. Commit the checkpoint first and publish READY last. The status object
        # is the small document the agent reads at the boundaries of each turn.
        completed_batches = dict(checkpoint["processed_batches"])
        completed_batches[batch_id] = batch_hash

        write_json(
            s3,
            bucket,
            "state/checkpoint.json",
            {
                "sequence": max(checkpoint["sequence"], batch["sequence"]),
                "records": current_records,
                "processed_batches": completed_batches,
                "guidance_hashes": guidance_hashes,
            },
        )

        source_dates = [record["date_received"] for record in active_records]

        status.update(
            status="READY",
            last_success_at=utc_now(),
            active_count=expected_count,
            source_date_min=min(source_dates, default=None),
            source_date_max=max(source_dates, default=None),
            ingestion_job_id=ingestion_job_id,
            changed_documents=len(changes),
        )

        write_json(s3, bucket, "state/pipeline.json", status)
        print(json.dumps(status, indent=2))
        return status

    except Exception as error:
        # Retain partial outputs for a deterministic retry. Marking FAILED blocks
        # data-backed agent answers until recovery completes successfully.
        status.update(
            status="FAILED",
            error=str(error),
            failed_at=utc_now(),
        )
        write_json(s3, bucket, "state/pipeline.json", status)
        raise


def main():
    """Read Glue arguments and start one ingestion run.

    Glue supplies its own extra arguments. parse_known_args() accepts those while
    still requiring our configuration URI. Omitting BATCH_KEY selects the next
    batch automatically; an explicit value is useful for the replay demonstration.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--CONFIG_S3_URI", required=True)
    parser.add_argument(
        "--BATCH_KEY",
        default=None,
        help="Omit to select the next queued batch.",
    )
    arguments, _ = parser.parse_known_args()

    config = load_config(arguments.CONFIG_S3_URI)
    ingest(config, arguments.BATCH_KEY)


if __name__ == "__main__":
    main()

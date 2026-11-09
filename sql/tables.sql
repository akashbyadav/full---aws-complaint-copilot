-- Replace ACCOUNT_ID with your 12-digit AWS account ID. All other names are fixed.
-- Run each of the following statements separately in the Athena console.
CREATE DATABASE IF NOT EXISTS complaint_copilot;

-- There is one deterministic Parquet object per input batch. The classroom
-- dataset is small, so an unpartitioned table keeps catalog setup straightforward.
CREATE EXTERNAL TABLE IF NOT EXISTS complaint_copilot.complaint_versions (
  complaint_id string,
  date_received string,
  company string,
  product string,
  issue string,
  state string,
  company_response string,
  timely_response string,
  has_narrative boolean,
  record_version bigint,
  is_deleted boolean,
  is_demo_fixture boolean,
  batch_id string
)
STORED AS PARQUET
LOCATION 's3://complaint-copilot-851725469799-us-east-1/curated/complaint_versions/';

-- Rank ALL versions first; filtering deletions before ranking would resurrect
-- an older version of a deleted complaint. Incoming versions are strictly ordered.
CREATE OR REPLACE VIEW complaint_copilot.complaints_current AS
SELECT complaint_id, CAST(date_received AS DATE) AS date_received,
       company, product, issue, state, company_response, timely_response,
       has_narrative, record_version, is_demo_fixture, batch_id
FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY complaint_id ORDER BY record_version DESC
  ) AS version_rank
  FROM complaint_copilot.complaint_versions
)
WHERE version_rank = 1 AND is_deleted = false;

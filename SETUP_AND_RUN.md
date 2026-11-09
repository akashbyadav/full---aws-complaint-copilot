# Step-by-step AWS setup and execution

You will create the AWS resources **manually**, in dependency order. Complete each step and check the expected result before proceeding.

Before starting, confirm that you have a working AWS account and local AWS CLI authentication. Use that existing setup to create resources, upload files and run Glue jobs through the **AWS Console** using the steps below.

The policy JSON, Athena SQL, Glue settings, Glue wheel, and AgentCore ZIP are **already supplied**. Use your local terminal to install the project dependencies and run the chat interface. Initial setup does not require generating setup files or building packages.

The supplied files contain the prepared data and application code. You will provision the AWS resources in your account and validate the deployment using the checks below.

## 0. What you will create

Use **US East (N. Virginia), us-east-1**, for all regional resources.

**Only `ACCOUNT_ID` is a placeholder.** Replace that exact token with your own 12-digit AWS account ID wherever you paste a console value, policy or SQL. Keep every other name as supplied. S3 still checks global bucket-name availability during creation.

| Resource | Exact name / value |
|---|---|
| S3 bucket | `complaint-copilot-ACCOUNT_ID-us-east-1` |
| Knowledge base | `complaint-copilot-kb` |
| S3 knowledge-base data source | `complaint-documents` |
| Athena workgroup | `complaint-copilot` |
| Glue Data Catalog database | `complaint_copilot` |
| History table / active view | `complaint_versions` / `complaints_current` |
| Glue job / scheduled trigger | `complaint-copilot-ingest` / `complaint-copilot-daily` |
| Glue service role | `ComplaintCopilotGlueRole` |
| AgentCore service role | `ComplaintCopilotRuntimeRole` |
| AgentCore runtime | `complaint_copilot` |
| Runtime entry point | `runtime_entrypoint.py` |

Bedrock's quick-create wizard creates the KB service role and OpenSearch collection/index. You do not need to enter those service-generated identifiers in the project configuration. Select **OpenSearch Serverless** explicitly in step 5.

| Service | What you create | Purpose |
|---|---|---|
| S3 | One private general-purpose bucket | Input, Parquet, documents, state, sessions, query results and code |
| Bedrock Knowledge Bases | One S3-backed knowledge base and data source | Managed chunking, embeddings and incremental synchronization |
| OpenSearch Serverless | One vector-search collection and index, quick-created by Bedrock | Hybrid keyword/vector retrieval |
| Bedrock models | Nova Pro, Titan Embeddings V2 and Cohere Rerank 3.5 | Tool decisions/answers, embeddings and reranking |
| Glue Data Catalog | One database, one table and one view through Athena | SQL schema |
| Athena | One workgroup using engine version 3 | Run constrained analytical SQL |
| Glue | One Python shell job and, later, a scheduled trigger | Daily batch processing |
| IAM | Separate Glue, knowledge-base and runtime roles | Service permissions |
| AgentCore Runtime | One Python runtime | Host the conversational agent |
| CloudWatch Logs | Service-created log groups | See failures and job/runtime output |

The agent and data processing run in AWS. The Streamlit client runs locally. Keep the account, bucket and data source dedicated to this project so cleanup is straightforward.

OpenSearch and the other services incur charges. Check the console's capacity/pricing choices before creating the collection. Deleting the knowledge base alone does not necessarily delete its OpenSearch collection.

## 1. Use your existing AWS access and prepare project Python

Open your existing AWS Console session, select **US East (N. Virginia), us-east-1**, and copy your 12-digit account ID from the account menu. Your existing identity must be allowed to create the project resources and pass their service roles.

**Use an IAM identity for setup, not the account root user.** AWS does not support knowledge-base creation as root. If your current console session is root, create an IAM user with console access and the required setup permissions, then sign in as that user. In a dedicated development account, `AdministratorAccess` can supply the setup permissions, but it grants broad account access; enable MFA and keep the application service roles scoped as described in step 7. No SSO configuration is required for this console-user path. [AWS knowledge-base creation requirement](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-create.html).

Changing the browser's signed-in user does not change the local CLI identity. If your existing local credentials belong to root, switch local authentication to a non-root identity before running the chat. Console-only access does not require creating access keys.

The Python AWS SDK uses your existing local AWS authentication. Inside AWS, the Glue job and AgentCore runtime use the service roles created in step 7. **Keep AWS access keys out of the project configuration.**

You need Python **3.11** locally for the chat interface. Installing its libraries is project preparation, not AWS account setup.

Open a local terminal in the directory containing the downloaded `aws-complaint-copilot` folder, then enter the project and confirm your Python version:

```bash
cd aws-complaint-copilot
python3.11 --version
```

If your terminal is already inside `aws-complaint-copilot`, skip the `cd` command.

**Keep terminals in this project root.** Python commands use `python -m package.module` so imports work across folders. Start the UI with `python -m streamlit run app/chat.py`.

Prepare the project's Python environment once. If it is already prepared, just activate it with `source .venv/bin/activate`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

**Expected:** the project dependencies are available, your existing AWS access is unchanged, and you have recorded the account ID for step 6. Project code reads `us-east-1` from `config/config.json`; select the same region in the console.

## 2. Inspect the included data

The prepared inputs are already included. Open `data/batches/batch-001.json` in your editor to inspect the batch ID, sequence and complaint records. Each record retains its original complaint date and source URL.

Expect 1,200 sampled records and active counts of 960, 1,200, and 1,199 across the three batches. The cohort contains 551 published narratives. The extracted guidance is in `data/guidance/articles.json`.

For setup, use `data/batches/` and `data/guidance/articles.json`. The original ZIP and HTML downloads are not required. The optional `data_pipeline/prepare_data.py` script can download the sources and rebuild the inputs if needed; do not rerun preparation during setup.

Do not change the sample size after loading the first batch into AWS. Completed batches are immutable. To use a different cohort, start with a fresh project bucket and knowledge base.

## 3. Create the S3 bucket

1. Open the AWS Console and select **N. Virginia / us-east-1**.
2. Open **S3 → Create bucket**.
3. Bucket name: **`complaint-copilot-ACCOUNT_ID-us-east-1`**. Replace only `ACCOUNT_ID` with your 12-digit account ID.
4. Use a general-purpose bucket.
5. Keep **Block all public access** enabled.
6. Keep ACLs disabled / bucket-owner-enforced ownership.
7. Use default **SSE-S3** encryption for this deployment.
8. Leave versioning disabled for the simple cleanup procedure in this guide.
9. Create the bucket.

Use this same bucket name everywhere below. Keep the suffix unchanged: the prebuilt code derives this exact name from your AWS account. If S3 reports that it already belongs to you, verify that it is the dedicated project bucket before reusing it.

Open the bucket's **Objects** tab, choose **Create folder**, enter `knowledge`, and create it. The prefix is initially empty; it will be selected as the Bedrock data source in step 5.

**Expected:** a private bucket in us-east-1. S3 is not the vector store; the next step creates OpenSearch for that purpose.

## 4. Verify model availability

Open **Amazon Bedrock** in N. Virginia. Locate the model catalog / model access controls and confirm your account can use:

| Purpose | Selection |
|---|---|
| Generation and tool calling | Amazon Nova Pro, regional model ID `amazon.nova-pro-v1:0` |
| Embeddings | Amazon Titan Text Embeddings V2, `amazon.titan-embed-text-v2:0` |
| Reranking | Cohere Rerank 3.5, `cohere.rerank-v3-5:0` |

Model access and quotas can depend on your account and organization policies. Invoke Nova Pro once in the playground in **us-east-1** before continuing. The code uses the regional foundation-model ID above, and the supplied IAM policy limits model invocation to this region.

For Cohere Rerank 3.5, complete any first-use model-access requirements through your setup identity. Cohere is a third-party model; the identity enabling access may need AWS Marketplace subscription permissions and the account's billing prerequisites. Complete this account-level setup before using the restricted runtime role. You do not need a separate Cohere API key. See [Bedrock model-access requirements](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).

**Regional configuration:** Cohere Rerank 3.5 is supported in **us-east-1**. Keep the exact regional Nova Pro model ID shown in the table to keep generation in the same region. [Supported reranking regions](https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-supported.html), [Nova Pro regional availability](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-pro.html).

## 5. Create the knowledge base and OpenSearch collection

1. Open **Bedrock → Knowledge Bases (KB)** and expand the arrow beside **Create Managed KB**.
2. Under **Self-managed KB**, choose **Unstructured Vector Store KB**. This option supports the complaint/guidance text and the selected OpenSearch Serverless vector store. Bedrock handles the configured chunking, embeddings and synchronization. The project's Athena SQL tool operates separately from the **Structured Data Store KB** option.
3. Name it `complaint-copilot-kb`.
4. Let Bedrock create a new service role. Record that role's name.
5. Choose **Amazon S3** as the data source.
6. Name the data source `complaint-documents`.
7. Set the S3 URI to `s3://complaint-copilot-ACCOUNT_ID-us-east-1/knowledge/`, replacing only `ACCOUNT_ID`.
8. Use the standard/default parser; the pipeline supplies clean text.
9. Choose **fixed-size chunking**, **400 tokens**, **10% overlap**.
10. Use the **DELETE** data-deletion policy.
11. Choose **Titan Text Embeddings V2**, **floating-point embeddings**, **1,024 dimensions**.
12. Choose **Quick create a new vector store → Amazon OpenSearch Serverless**.
13. Review the collection capacity and cost settings. Complete creation and wait until the knowledge base and collection are available.
14. Confirm the KB name is **complaint-copilot-kb** and the source name is **complaint-documents**. Keep the overview available to inspect the linked OpenSearch collection and service role; there is no ID to copy into the project.

Quick create configures the vector index and required fields. The index needs the FAISS engine and a filterable text field for the configured metadata filters and hybrid search. [AWS creation guide](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-create.html), [hybrid-search configuration](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-config.html).

Keep the embedding dimension and chunking configuration consistent throughout setup. A chunking-strategy change requires a new data source; an incompatible embedding/index change requires rebuilding the index.

There are no documents to sync yet. The Glue job will create them and start synchronization. Do not point the knowledge base at the bucket root.

**Expected:** a knowledge base explicitly showing OpenSearch Serverless as its vector store and `knowledge/` as its source prefix.

## 6. Use the prepared setup files

Use the supplied files below. In your editor, replace `ACCOUNT_ID` with your 12-digit account ID in the text files under `deployment/iam/`, `deployment/glue/`, `deployment/agentcore/`, and in `sql/tables.sql`. Save them. Files without that token need no changes.

| Prepared file | Where you use it |
|---|---|
| `deployment/iam/glue-trust.json` | Glue role → custom trust policy |
| `deployment/iam/glue-policy.json` | Glue role → inline permissions |
| `deployment/iam/runtime-trust.json` | AgentCore role → custom trust policy |
| `deployment/iam/runtime-policy.json` | AgentCore role → inline permissions |
| `deployment/iam/kb-rerank-policy.json` | Bedrock-created KB role → inline permissions |
| `deployment/iam/chat-caller-policy.json` | Optional restricted local identity → find/invoke the runtime |
| `sql/tables.sql` | Athena query editor; run three statements separately |
| `deployment/glue/job-arguments.json` | Copy each key/value into the Glue console job parameters |
| `deployment/agentcore/runtime-settings.json` | Reference for the exact AgentCore console settings in step 13 |
| `deployment/artifacts/complaint_copilot_shared-0.1.0-py3-none-any.whl` | Upload directly to S3 `code/complaint_copilot_shared-0.1.0-py3-none-any.whl` |
| `deployment/artifacts/agent.zip` | Upload directly to S3 `runtime/agent.zip` |

**Leave `config/config.json` and both deployment packages unchanged.** Configuration already contains the selected resource names, models and query settings. It contains no credentials. At execution, the code gets its account ID from AWS STS and finds the AWS-assigned KB/source/runtime identifiers by their exact names. Your local AWS credentials and deployed service roles must belong to the same project account. No repackaging is needed.

These are small read-only lookups: [GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html), [ListKnowledgeBases](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_ListKnowledgeBases.html), [ListDataSources](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_ListDataSources.html), and [ListAgentRuntimes](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_ListAgentRuntimes.html). Missing or duplicate names produce a clear error instead of selecting an arbitrary resource.

**Permission scope:** the supplied policies specify the bucket paths, workgroup, catalog objects, models, region and account. KB permissions use `knowledge-base/*` within your account and us-east-1 because AWS assigns KB IDs at creation. This permits the listed operations on all KBs in that account/region; a name match in Python is not an IAM restriction. List APIs and reranking use `Resource: "*"` where appropriate. These asterisks are IAM wildcards, not values to replace. The chat-caller policy permits only runtime discovery/invocation; local agent mode additionally requires application permissions, as explained in step 12.

The live-guidance tool reads its approved topic IDs and URLs from **`data/guidance/articles.json`**. This is prepared source data, not a separate setup configuration. Leave it unchanged.

### 6.1 Which deployment packages does this project use?

There are two prepared upload files: a **Python wheel (.whl) for Glue Python shell**, and a **ZIP for AgentCore**. A wheel is a packaged Python library with installation metadata. You can upload both supplied files directly.

| Local package | Used by | Contents | S3 destination |
|---|---|---|---|
| `deployment/artifacts/complaint_copilot_shared-0.1.0-py3-none-any.whl` | Glue ingestion job | Shared Python package plus wheel metadata | `s3://complaint-copilot-ACCOUNT_ID-us-east-1/code/complaint_copilot_shared-0.1.0-py3-none-any.whl` |
| `deployment/artifacts/agent.zip` | AgentCore Runtime | Agent code, shared code, config, guidance articles and runtime libraries | `s3://complaint-copilot-ACCOUNT_ID-us-east-1/runtime/agent.zip` |

Upload these existing files at steps 9 and 13. Replace `ACCOUNT_ID` in the S3 destination only. Keep the wheel's complete filename unchanged.

### 6.2 Inside the Glue wheel — the shared helper package

The supplied file is **`complaint_copilot_shared-0.1.0-py3-none-any.whl`**. Load this Python shell library through **Python library path / `--extra-py-files`**. See [AWS Glue Python shell custom libraries](https://docs.aws.amazon.com/glue/latest/dg/add-job-python.html).

Its layout is:

```text
complaint_copilot_shared-0.1.0-py3-none-any.whl
├── shared/
│   ├── __init__.py
│   ├── aws_helpers.py
│   └── resource_lookup.py
└── complaint_copilot_shared-0.1.0.dist-info/
    ├── METADATA
    ├── WHEEL
    ├── top_level.txt
    └── RECORD
```

| Included item | Why it is included |
|---|---|
| `shared/__init__.py` | Makes `shared` an importable Python package |
| `shared/aws_helpers.py` | Reads configuration, accesses S3, runs Athena queries, and supplies common status/timestamp helpers |
| `shared/resource_lookup.py` | Finds the knowledge base and data source using their configured names |
| `.dist-info/` metadata | Describes the library name, version, Python compatibility and packaged files to the installer |

Glue starts the separately uploaded **`code/ingest.py`** script. The job loads the wheel specified by **`--extra-py-files`**, making `shared.aws_helpers` and `shared.resource_lookup` available to that script. The distribution is named `complaint-copilot-shared`, but the Python import remains **`shared`**.

The wheel contains the project's shared source files. Glue installs **`boto3==1.40.70`** and **`pyarrow==20.0.0`** separately using **`--additional-python-modules`**. Keep those package pins in that parameter; put the S3 wheel URI in **`--extra-py-files`**. The script, config and input data are uploaded separately in step 9.

The filename suffix **`py3-none-any`** identifies a pure-Python wheel with no platform-specific native binaries. The package requires Python 3.9 or newer. The packaging program includes the shared source files and the required installation metadata in this wheel. Keep its complete filename unchanged when uploading it.

### 6.3 Inside agent.zip — the complete agent application

This archive includes the following application files. The library folders shown below are representative; the archive also contains their supporting dependencies and package metadata.

```text
agent.zip
├── runtime_entrypoint.py
├── conversational_agent/
│   ├── __init__.py
│   ├── agent.py
│   ├── tools.py
│   └── runtime.py
├── shared/
│   ├── __init__.py
│   ├── aws_helpers.py
│   └── resource_lookup.py
├── config/
│   └── config.json
├── data/
│   └── guidance/
│       └── articles.json
├── bedrock_agentcore/
├── boto3/
├── botocore/
├── bs4/
├── sqlglot/
└── Other installed dependency folders and package metadata
```

| Included item | Purpose inside the deployed runtime |
|---|---|
| `runtime_entrypoint.py` | Root launcher selected in the AgentCore console; imports and calls `conversational_agent.runtime.main()` |
| `conversational_agent/runtime.py` | Starts the AgentCore HTTP application and registers the conversation handler |
| `conversational_agent/agent.py` | Handles conversation history, model/tool decisions and the final answer |
| `conversational_agent/tools.py` | Implements SQL analysis, knowledge-base retrieval and live guidance fetching |
| `shared/` | Provides the same configuration, AWS operations and name lookups used locally |
| `config/config.json` | Supplies fixed resource names, model IDs, retrieval settings and timeouts |
| `data/guidance/articles.json` | Supplies the live tool's approved topic IDs and URLs; the separate ingestion upload supplies the saved guidance to the KB |
| Installed libraries | Makes the application runnable without your local virtual environment |

The runtime libraries are installed from **`deployment/requirements-runtime.txt`**: `boto3`, `beautifulsoup4`, `sqlglot`, and `bedrock-agentcore`, plus their dependencies. For example, the installed import folder for `beautifulsoup4` is named `bs4`. Including only the requirements text file would not install those libraries in this project's ZIP deployment.

The package targets **Linux ARM64, Python 3.11**. A Mac or Windows virtual environment may contain native binaries for the wrong operating system or architecture, so the packaging program downloads compatible wheels explicitly. AWS documents ARM64 packaging for direct code deployment in its [AgentCore Python deployment guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html).

**`runtime_entrypoint.py` must be at the ZIP root.** The archive must open directly to the tree above, without an outer `build/runtime/` or `aws-complaint-copilot/` folder. The shared source files are included directly under `shared/`; `agent.zip` does not contain a nested Glue wheel.

The runtime archive excludes `app/`, `data_pipeline/`, raw downloads, complaint batches, `.venv/`, and IAM policies. Streamlit runs on your local machine; ingestion runs in Glue; the deployed agent queries the AWS data stores.

### 6.4 Optional: recreate the packages after changing the code

Follow this section when you need to rebuild the packages after changing the source code. You can skip it when using the supplied wheel and ZIP.

1. Open a terminal in the project root, using the folder from step 1. Activate the Python 3.11 environment already prepared there:

   ```bash
   source .venv/bin/activate
   python --version
   ```

2. Run the existing packaging program. It requires internet access to download Python libraries, but does not call AWS, upload files, or create resources:

   ```bash
   python -m deployment.package_runtime
   ```

3. Follow what [deployment/package_runtime.py](deployment/package_runtime.py) does, in order:

   - Copies `shared/` and `deployment/glue/pyproject.toml` into `build/glue-library/`, then uses the pinned build tools to create `deployment/artifacts/complaint_copilot_shared-0.1.0-py3-none-any.whl` with installation metadata.
   - Clears and recreates the temporary `build/runtime/` staging folder.
   - Installs libraries from `deployment/requirements-runtime.txt` into that staging folder, targeting Linux ARM64 and Python 3.11.
   - Copies `conversational_agent/`, `shared/`, `config/config.json`, and `data/guidance/articles.json` into the staging folder.
   - Writes `runtime_entrypoint.py` into the staging folder's root.
   - Compresses the **contents** of the staging folder into `deployment/artifacts/agent.zip`, without the outer staging-folder path.

4. Wait for the final **“Prepared Glue and AgentCore upload files in:”** message. The two output paths are the same ones listed in section 6.1. Rebuilding overwrites the local wheel and ZIP. If dependency installation fails, fix the reported error and rerun; an existing `agent.zip` could still be from an older successful build.

5. Optionally inspect the archives without extracting or modifying them:

   ```bash
   python -m zipfile -l deployment/artifacts/complaint_copilot_shared-0.1.0-py3-none-any.whl
   python -m zipfile -l deployment/artifacts/agent.zip
   ```

   The first listing must show the three `shared/` paths and the wheel metadata described in section 6.2. The second listing is much longer because it contains installed libraries; check that `runtime_entrypoint.py`, the application folders, configuration and article file have the paths shown in section 6.3.

6. Upload the required files through the S3 console. Local rebuilding alone does not update running AWS resources:

   | What you changed | What to upload / update |
   |---|---|
   | `shared/` Python code | Upload the rebuilt wheel and agent ZIP to their existing S3 keys; update the AgentCore code deployment; subsequent Glue runs load the updated `complaint_copilot_shared-0.1.0-py3-none-any.whl` |
   | `conversational_agent/` code or runtime dependencies | Upload rebuilt `agent.zip`; update the existing AgentCore runtime's code deployment |
   | Only `data_pipeline/ingest.py` | Upload that script to `code/ingest.py`; no package rebuild is needed for this script alone |
   | `config/config.json` | Upload it to `code/config.json`; rebuild/upload `agent.zip` and update AgentCore so both services receive the intended settings |

   For AgentCore, replace `runtime/agent.zip` in S3, then open the existing **`complaint_copilot`** runtime and update its code artifact using the console. Keep Python 3.11 and `runtime_entrypoint.py`, save/deploy the update, and wait for **Ready**. Merely overwriting the S3 ZIP does not redeploy the running agent. Start a **new conversation** when checking the updated deployment.

To rebuild only the Glue wheel after changing shared code, without downloading or rebuilding the AgentCore package, run:

```bash
python -m deployment.package_runtime --glue-only
```

The full command above remains necessary when shared-code changes must also reach AgentCore.

## 7. Create the service roles

### 7.1 Glue role

1. Open **IAM → Roles → Create role**.
2. Select **Custom trust policy**.
3. Paste `deployment/iam/glue-trust.json`.
4. Attach the AWS-managed **AWSGlueServiceRole** policy for Glue service/logging needs.
5. Name the role **ComplaintCopilotGlueRole**.
6. Open the role → **Add permissions → Create inline policy → JSON**.
7. Paste `deployment/iam/glue-policy.json`.
8. Name the inline policy `ComplaintCopilotDataAccess`.

This role writes only this project's ingestion prefixes, synchronizes the configured knowledge base, and runs reconciliation queries in the project workgroup.

### 7.2 AgentCore runtime role

1. Create another role using **Custom trust policy**.
2. Paste `deployment/iam/runtime-trust.json`.
3. Name it **ComplaintCopilotRuntimeRole**.
4. Add an inline policy using `deployment/iam/runtime-policy.json`.
5. Name the policy `ComplaintCopilotRuntimeAccess`.

This role can read the dataset and pipeline status, invoke the approved models, retrieve knowledge-base content, execute Athena reads, and write query results/session history. It does not receive permission to overwrite the curated complaint dataset.

### 7.3 Knowledge-base reranking permission

1. Open **Bedrock → Knowledge Bases → complaint-copilot-kb**. Locate the **Service role / IAM role ARN** in the knowledge-base details and open that role in IAM. The role created by the wizard normally starts with **AmazonBedrockExecutionRoleForKnowledgeBase_**.
2. In that role's **Permissions** tab, choose **Add permissions → Create inline policy → JSON**.
3. Paste the complete contents of `deployment/iam/kb-rerank-policy.json`. It grants `bedrock:Rerank` and `bedrock:InvokeModel` for Cohere Rerank 3.5 in us-east-1. This file needs no account-ID replacement.
4. Name the policy **ComplaintCopilotRerank** and create/save it. If that inline policy already exists on this role, edit its JSON instead.
5. Keep the automatically created S3, embedding and OpenSearch policies. Add the reranking policy under **Permissions**; the trust policy controls a different responsibility.

For the application's `Retrieve` request with reranking, **the knowledge-base service role performs reranking**. Adding permissions only to the AgentCore execution role or your console user does not grant them to the KB role. The supplied runtime policy also allows reranking, but the KB-role policy is still required. [AWS permissions for reranking](https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-prereq.html).

If a question fails with **AccessDeniedException: bedrock:Rerank**, the error's assumed-role ARN identifies the role to update. Open the IAM role named between `assumed-role/` and the next slash, then apply the policy above. A `BedrockReranking-...` suffix is the temporary session name, not another IAM role. After saving, allow the IAM change to propagate and retry the same question. This permission change does not require another Glue run or knowledge-base synchronization.

**Expected:** three separate service roles: Bedrock-created KB role, Glue role, runtime role. Your own operator identity still needs permission to create/run these services and pass the roles. A service role is not a login identity.

## 8. Create the Athena workgroup and schema

### 8.1 Workgroup

1. Open **Athena → Workgroups → Create workgroup**.
2. Name: **complaint-copilot**.
3. Use **Athena SQL**, engine version **3**.
4. Set query results to `s3://complaint-copilot-ACCOUNT_ID-us-east-1/athena-results/`.
5. Use SSE-S3 result encryption.
6. Enable **Override client-side settings / enforce workgroup configuration**.
7. Enable CloudWatch metrics.
8. Set a per-query **data scanned limit of 100 MB**.
9. Create the workgroup.

This is the scan-cost control. The code also limits returned rows and cancels queries after its timeout.

### 8.2 Database, table and view

1. Open the Athena query editor.
2. Select workgroup **complaint-copilot**.
3. Select data source **AwsDataCatalog**.
4. Open `sql/tables.sql` locally.
5. Run its **CREATE DATABASE** statement by itself.
6. Select database **complaint_copilot**.
7. Run the **CREATE EXTERNAL TABLE** statement by itself.
8. Run the **CREATE OR REPLACE VIEW** statement by itself.

Do not paste all three statements into a single Athena query.

Run:

```sql
SELECT COUNT(*) AS complaint_count FROM complaints_current;
```

**Expected:** 0, because no Parquet batch has been written yet. The catalog should contain `complaint_versions` and `complaints_current`.

If your account uses Lake Formation grants instead of IAM-only catalog access, grant the Glue role, runtime role and operator identity DESCRIBE/SELECT access to this database/table/view and the required underlying S3 location. IAM permissions alone do not override Lake Formation restrictions.

## 9. Upload the code and initial data

The contents and purpose of the supplied Glue wheel are explained in [section 6.2](#62-inside-the-glue-wheel--the-shared-helper-package). Upload the `.whl` file intact.

Use **S3 → your bucket → Objects**. Create the `code/`, `raw/batches/` and `raw/guidance/` folders. For nested folders, first create/open `raw/`, then create `batches/` and `guidance/` inside it.

Open each destination folder, choose **Upload → Add files**, select the local file, and choose **Upload**:

| Local file to select | S3 object key after upload |
|---|---|
| `data_pipeline/ingest.py` | `code/ingest.py` |
| `deployment/artifacts/complaint_copilot_shared-0.1.0-py3-none-any.whl` | `code/complaint_copilot_shared-0.1.0-py3-none-any.whl` |
| `config/config.json` | `code/config.json` |
| `data/guidance/articles.json` | `raw/guidance/articles.json` |
| `data/batches/batch-001.json` | `raw/batches/batch-001.json` |

Check the object keys in the console after uploading. Select the individual files, so you do not accidentally upload extra local folder levels.

Upload only batch 1 at this step. You will upload batches 2 and 3 later to verify incremental arrivals, corrections and deletion against their expected results.

Upload only the files listed in the table. Keep the local virtual environment and any optional source downloads on your local machine.

**Expected:** one input batch, the article JSON, and three code/config objects in S3. `knowledge/` and `curated/` are still empty.

## 10. Create the Glue ingestion job

1. Open **AWS Glue → ETL jobs**.
2. Create a **Python shell script** job. The console may place this under “Script editor” or “Python shell.”
3. Choose an existing script from S3: `s3://complaint-copilot-ACCOUNT_ID-us-east-1/code/ingest.py`.
4. Name: **complaint-copilot-ingest**.
5. Role: **ComplaintCopilotGlueRole**.
6. Python shell version: **3.9**.
7. Capacity: **0.0625 DPU** for this small input. If your environment encounters a memory limit, choose 1 DPU.
8. Disable the preloaded analytics library set; use **library-set = none**.
9. Set **maximum concurrent runs = 1**.
10. Set **job timeout = 60 minutes**.
11. Set **maximum retries = 1**.
12. Add the parameters below. The exact values are also in `deployment/glue/job-arguments.json`.

| Parameter | Value |
|---|---|
| `--CONFIG_S3_URI` | `s3://complaint-copilot-ACCOUNT_ID-us-east-1/code/config.json` |
| `--extra-py-files` | `s3://complaint-copilot-ACCOUNT_ID-us-east-1/code/complaint_copilot_shared-0.1.0-py3-none-any.whl` |
| `--additional-python-modules` | `boto3==1.40.70,pyarrow==20.0.0` |
| `--library-set` | `none` |

Replace only `ACCOUNT_ID` in these S3 URIs. Do not add `--BATCH_KEY` as a permanent job default: without it, the job selects the next batch.

If the console provides a **Python library path** field instead of exposing `--extra-py-files`, enter the full S3 wheel URI there. Inspect the saved default arguments to confirm it becomes `--extra-py-files`. Use the supplied `.whl` file intact, including its original filename. Set this library URI once, without conflicting values in the library-path field and job parameters.

Python shell 3.9 and the two compatible package pins are used **only inside Glue**. Local code and AgentCore use Python 3.11 and their separate requirement files. Do not install the complete Streamlit/AgentCore requirements into Glue.

Leave the VPC connection unset for this setup. A private subnet would require properly configured routes/endpoints for S3, Bedrock, Athena, and dependency downloads.

**Expected:** one saved Python shell job with the correct source script, shared package wheel and configuration URI.

### 10.1 Recover from “No module named shared”

This error occurs while the script imports the helper package, before its ingestion function runs. Confirm that the prepared wheel described in section 6.2 is uploaded and configured as the job's Python library.

1. In **S3 → project bucket → code/**, upload the local file **`deployment/artifacts/complaint_copilot_shared-0.1.0-py3-none-any.whl`** intact, preserving its filename.
2. Open **Glue → complaint-copilot-ingest → Job details → Advanced properties**. Set **Python library path** (the `--extra-py-files` argument) to `s3://complaint-copilot-ACCOUNT_ID-us-east-1/code/complaint_copilot_shared-0.1.0-py3-none-any.whl`, replacing only the account ID. Use one library-path setting, without conflicting duplicates.
3. Keep **Python shell, Python 3.9**, and keep `--additional-python-modules` as `boto3==1.40.70,pyarrow==20.0.0`. Choose **Save**, then **Run** to start a new execution.
4. Select the **new run → Input arguments** and verify that `--extra-py-files` shows the full wheel URI. An older failed run retains its original arguments. If import still fails, inspect **Logs** for the wheel download/install message and confirm the wheel object exists at that exact S3 key.

The Glue role's supplied `code/*` read permission already covers this file. Keep the input batch and state objects in place; the import error does not require resetting the pipeline. Updating the library artifact does not require an AgentCore deployment change.

## 11. Run the first ingestion

1. Open the Glue job and choose **Run**.
2. Open its **Runs** tab.
3. Inspect the run's output/error logs in CloudWatch.
4. Wait for **Succeeded**.

The job:

```text
Reads batch-001
  → validates 960 records
  → sets UPDATING
  → writes batch-001.parquet
  → writes available complaint narratives and guidance documents
  → starts Bedrock synchronization
  → waits for sync and retrieval visibility
  → checks Athena count
  → writes checkpoint and READY
```

The first run includes dependency installation, indexing and visibility checks, so allow several minutes. Completion time depends on account quotas and service load. A Glue success after calling only StartIngestionJob would be insufficient; this script actually waits and checks.

In **S3 → your bucket → state/**, select **pipeline.json**, choose **Download**, and open the downloaded JSON in your editor. The Glue job created this file; you do not edit it.

**Expected:** `status=READY`, `batch_id=batch-001`, `active_count=960`.

In Athena:

```sql
SELECT COUNT(*) AS complaint_count FROM complaints_current;
SELECT COUNT(*) AS narrative_count FROM complaints_current WHERE has_narrative = true;
SELECT company, COUNT(*) AS complaint_count
FROM complaints_current
GROUP BY company
ORDER BY complaint_count DESC, company
LIMIT 5;
```

Expected first two results: **960** and **431**.

In the knowledge base's test console, retrieve “disputing a credit card charge.” Inspect the retrieved text and metadata. Try **Hybrid** search. Enable Cohere Rerank 3.5 in query settings if using the console. The application enables it through its API request.

An ingestion error may leave partial outputs. Do not delete the checkpoint or raw input. Fix the reported cause and rerun the job; it resumes the same batch.

## 12. Optional local agent check — you may skip to step 13

This optional check runs the agent's Python code on your local machine using your existing AWS access. The deployed path is step 13, where AgentCore runs that code. Skip this check if you want to go directly to the AWS-hosted agent.

```bash
python -m streamlit run app/chat.py
```

Open the localhost address shown in the terminal. Select **Local Python**.

Ask:

- “How many active complaints are in the loaded sample?”
- “Which five companies have the most complaints?”
- “For the first company, show examples of the reported experiences.”
- “What does the saved CFPB guidance say about disputing a charge?”
- “Fetch the current official guidance about disputing a charge.”

Expand **Sources, SQL, tools, and freshness** after an answer. Confirm the query ID, filters, retrieved passages, source references and last successful refresh.

Local mode still calls real AWS services and persists session turns in S3. Your operator identity therefore needs the corresponding read/model/Athena/session permissions. If you use a restricted operator identity, attach the needed scoped permissions from the runtime policy and the additional setup permissions approved for your account.

If a public webpage refuses the live request, the tool reports the failure. Saved guidance retrieval remains a separate available path; the agent must not present that snapshot as a successful live fetch.

## 13. Deploy the agent to AgentCore Runtime

### 13.1 Upload the supplied runtime package

The ready-to-upload file is **`deployment/artifacts/agent.zip`**. Do not build or unzip it for setup. It already includes the application, configuration, guidance article file, and Linux ARM64 dependencies for Python 3.11.

See [section 6.3](#63-inside-agentzip--the-complete-agent-application) for the archive layout and each component's purpose, or [section 6.4](#64-optional-recreate-the-packages-after-changing-the-code) to rebuild it after editing the code.

The archive preserves `conversational_agent/`, `shared/`, `config/` and `data/guidance/articles.json`. Its root launcher is `runtime_entrypoint.py`. The original source archive, replay batches, Streamlit and ETL libraries are excluded.

In **S3**, create/open `runtime/` inside `complaint-copilot-ACCOUNT_ID-us-east-1`. Choose **Upload → Add files**, select **`deployment/artifacts/agent.zip`**, and upload it. The S3 URI is `s3://complaint-copilot-ACCOUNT_ID-us-east-1/runtime/agent.zip`, with your account ID substituted.

### 13.2 Create the runtime

Use **Bedrock AgentCore → Runtime / Host agent** in N. Virginia:

1. Choose **direct code / ZIP deployment**.
2. Choose the S3 source `s3://complaint-copilot-ACCOUNT_ID-us-east-1/runtime/agent.zip`.
3. Agent name: **complaint_copilot**.
4. Runtime: **Python 3.11**.
5. Entry point: **runtime_entrypoint.py**.
6. Execution role: **ComplaintCopilotRuntimeRole**.
7. Network mode: **Public** so the process can reach AWS endpoints and official webpages.
8. Use **IAM authorization** for invocation. Public network mode does not mean anonymous API access.
9. Create and wait for **Ready**.
10. Confirm the default endpoint is available. The UI finds this runtime by name; there is no ARN to paste into configuration.

Use the existing ZIP in S3 as the code artifact. The console wording can vary; select **direct code / ZIP**, with Python 3.11 and the entry point above. See [AWS direct code deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html).

### 13.3 Connect Streamlit to the deployed runtime

Leave `config/config.json` unchanged. The UI finds **complaint_copilot** in **us-east-1** using your existing local AWS identity.

In the project terminal with `.venv` active, start the chat UI:

```bash
python -m streamlit run app/chat.py
```

Open the localhost address printed in the terminal, choose **AWS AgentCore**, and start a new conversation. If Streamlit was already running from the optional check, select **AWS AgentCore** in the sidebar.

Ask “How many active complaints are in the loaded sample?” and then “Show examples for the company with the most complaints.” Expand **Sources, SQL, tools, and freshness** to inspect the executed calls. You can also try the guidance questions in step 12.

Your local identity needs permission to find and invoke the runtime. For a restricted caller, attach `deployment/iam/chat-caller-policy.json` after replacing `ACCOUNT_ID`. The runtime name and endpoint patterns are already included. An existing administrator identity already has these permissions. The caller policy belongs to the identity running Streamlit, not the runtime execution role.

**Expected:** answers and traces appear while the agent code executes in AWS. The UI still runs locally.

For future code or configuration changes, follow the optional rebuild and redeployment instructions in [section 6.4](#64-optional-recreate-the-packages-after-changing-the-code). The supplied packages remain sufficient for initial setup.

## 14. Process the next daily arrival

In **S3 → your bucket → raw/batches/**, choose **Upload → Add files**, select local `data/batches/batch-002.json`, and upload it. Keep batch 1 in place.

In the Glue console, open `complaint-copilot-ingest` and choose **Run** again. No arguments need changing. It selects the next unprocessed batch. Wait for **Succeeded** before checking the results below.

Download the updated **state/pipeline.json** from the S3 console as in step 11. Then run these queries individually in the Athena console:

```sql
SELECT COUNT(*) AS complaint_count FROM complaints_current;
SELECT COUNT(*) AS narrative_count FROM complaints_current WHERE has_narrative = true;
```

**Expected:** `READY` for `batch-002`, **1,200** active complaints, **551** narratives.

Ask the count question again in the chat. It should use SQL and return 1,200 for this sample. Continue with duplicate replay before adding fixture batch 3.

## 16. Verify duplicate replay

A normal Run with no new input reports “No new batch.” To explicitly replay batch 2 through the console:

1. Ensure no Glue run is active. Keep the daily trigger disabled until step 19.
2. Open **Glue → ETL jobs → complaint-copilot-ingest → Job details**.
3. Under **Job parameters** (expand Advanced properties if needed), temporarily add `--BATCH_KEY` with value `raw/batches/batch-002.json`.
4. Choose **Save**, then **Run**. Inspect the run's CloudWatch output.
5. After the run finishes, remove `--BATCH_KEY` from Job parameters and **Save** again. The next normal run will then select the next pending batch automatically.

These are saved job parameters, so the final removal is required. [AWS job-parameter controls](https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-etl-glue-arguments.html).

**Expected:** the logs report the exact batch is already processed. Active count remains **1,200**. No extra SQL rows or narrative copies are introduced.

If you edit a completed batch and upload it under the same batch ID, the job rejects its changed checksum. Use a new batch for corrections.

## 17. Verify corrections and deletion

In **S3 → your bucket → raw/batches/**, upload local `data/batches/batch-003.json`. In the **Glue console**, confirm that the temporary `--BATCH_KEY` from step 16 has been removed, then choose **Run** for `complaint-copilot-ingest`.

After success, run:

```sql
SELECT COUNT(*) AS complaint_count FROM complaints_current;

SELECT complaint_id, record_version, is_demo_fixture
FROM complaints_current
WHERE complaint_id IN ('11335237', '11342596', '11343867');

SELECT COUNT(*) AS deleted_id_count
FROM complaints_current
WHERE complaint_id = '12059949';
```

Expected:

- Total active complaints: **1,199**.
- The three corrected IDs have version **3** and fixture flag **true**.
- The deleted ID's current count is **0**.

Ask the agent to find `ORCHID-CORRECTION`. Inspect the retrieved passages: they should contain an explicit label identifying the correction as synthetic. The agent should preserve that distinction in its explanation.

Replay batch 3 using the temporary console job parameter from step 16, this time setting its value to `raw/batches/batch-003.json`. Remove the parameter and save again after the run. The count remains **1,199**.

## 18. Optional controlled failure and recovery check

Perform this **before the first successful batch-3 run**, once batch 2 has reached `READY`. If you want to use this optional check, follow it before completing step 17.

1. Ensure no Glue run is active.
2. In your editor, use **Save As** to create `config/config.failure.json` from `config/config.json`. Keep the original configuration unchanged. Use this temporary copy only for the optional failure and recovery check.
3. In that copy only, add `"data_source_id": "ZZZZZZZZZZ"` as an extra JSON field, with the appropriate comma. This deliberately nonexistent ID overrides the normal name lookup to trigger a synchronization failure; it is not a setup placeholder.
4. In **S3 → your bucket → code/**, upload that temporary copy as `config.failure.json`.
5. In **raw/batches/**, upload batch 3 if it is not already present.
6. In **Glue → the job → Job details → Job parameters**, temporarily change `--CONFIG_S3_URI` to `s3://complaint-copilot-ACCOUNT_ID-us-east-1/code/config.failure.json` and add `--BATCH_KEY` with value `raw/batches/batch-003.json`.
7. Choose **Save**, then **Run**. Let the run and its configured retry finish.

**Expected:** synchronization fails; `state/pipeline.json` says `FAILED`; the agent refuses to present the partial refresh as ready. SQL/document files may already have changed, while the checkpoint still describes the previous successful batch.

To recover, open **Job details → Job parameters**, restore `--CONFIG_S3_URI` to `s3://complaint-copilot-ACCOUNT_ID-us-east-1/code/config.json`, remove `--BATCH_KEY`, and **Save**. Then choose **Run**. The job selects the failed batch and finishes it. Check `READY` and count **1,199**. Do not delete state files or manually mark the pipeline READY. You may remove the temporary failure-config copies after verifying recovery.

## 19. Enable the daily schedule

Only enable this after the manual runs work.

1. Open **Glue → Data Integration and ETL → Triggers** (console labels may vary).
2. Create a **Scheduled** trigger named `complaint-copilot-daily`.
3. Choose the job `complaint-copilot-ingest`.
4. Use cron expression `cron(0 2 * * ? *)`.
5. Activate the trigger.

This is **02:00 UTC / 07:30 Asia/Kolkata** daily. Glue cron schedules use UTC.

The upstream producer places the next `batch-NNN.json` in `raw/batches/` before the schedule. Each run processes one batch, or resumes a failed one. If several batches are queued, run the job repeatedly to catch up. With no new batch, it logs a no-op.

For a guidance-only refresh, upload the revised complete `raw/guidance/articles.json` catalog and a new correctly sequenced batch with `"records": []`. The job updates guidance without adding complaint rows.

The supplied archive replay is finite. The schedule does not fabricate new complaints or fetch new public narratives after the historical archive's cutoff.

## 20. Troubleshooting by symptom

| Symptom | What to inspect / fix |
|---|---|
| `AccessDenied` creating a service | Operator permission and `iam:PassRole`; service roles alone do not grant operator setup permission |
| KB creation fails on OpenSearch access | KB service role, collection data-access policy and network policy; quick create should configure them |
| KB accidentally uses S3 Vectors | Recreate it with OpenSearch Serverless selected; do not continue with a different vector store |
| Hybrid retrieval unsupported | Confirm OpenSearch Serverless, correct index, FAISS engine and filterable text field |
| Rerank model unavailable | Use us-east-1 and `cohere.rerank-v3-5:0`; complete Cohere first-use access and check KB/caller permissions |
| `Retrieve` fails with `AccessDeniedException` for `bedrock:Rerank` | Attach `deployment/iam/kb-rerank-policy.json` to the knowledge-base service role named in the error; follow section 7.3 and retry the question |
| Nova model access denied | Use the regional ID shown in step 4, the us-east-1 endpoint, and the region-scoped invocation policy; verify account access and quotas |
| `ModuleNotFoundError: No module named shared` | Upload the supplied wheel and follow section 10.1; confirm the new run’s Input arguments contain the full `.whl` URI under `--extra-py-files` |
| Glue cannot install packages | Use the exact Python 3.9 pins and leave the VPC connection unset for this setup; check outbound access |
| Glue says next sequence differs | An earlier batch is missing or unprocessed; process inputs in order |
| “Processed batch is immutable” | Keep original files; put the correction in a new batch ID/sequence |
| Athena table not found | Run all three SQL statements separately, with the correct database/workgroup |
| Athena authorization fails | IAM S3/catalog/workgroup permissions and, if enabled, Lake Formation grants |
| SQL count reconciliation fails | Inspect curated prefix for accidental extra files and the current-view definition |
| Sync COMPLETE but visibility waits | Allow OpenSearch propagation; inspect failed-document counts, metadata and source prefix |
| Agent says dataset not ready | Read `state/pipeline.json` and Glue logs; fix and rerun the failed batch |
| Agent returns a tool error | Expand the trace; distinguish SQL validation, model access, retrieval and website failures |
| AgentCore ZIP import error | Upload `deployment/artifacts/agent.zip` intact, select Python 3.11, and use `runtime_entrypoint.py` as the entry point |
| AgentCore invocation denied | Local identity needs the supplied chat-caller policy; inspect the runtime execution role separately |
| Named resource missing or duplicated | Check the account, us-east-1 region and exact names in step 0; retain one matching KB/source/runtime |
| Resource lookup AccessDenied | Use the supplied policies: Glue/runtime need ListKnowledgeBases; Glue also needs ListDataSources; local UI needs ListAgentRuntimes |
| Live website returns 403 or times out | The public source rejected the request; report the failure and use separately labelled saved guidance |
| Changed local config has no effect in AWS | Re-upload Glue config; rebuild/update the AgentCore ZIP for runtime config changes |

## 21. Cleanup

Delete only the resources you created for this dedicated project:

1. Deactivate and delete the daily Glue trigger.
2. Stop any running Glue job and delete `complaint-copilot-ingest`.
3. Delete the AgentCore runtime/endpoints through its console.
4. Delete the knowledge base and data source.
5. Open OpenSearch Serverless and delete the project's collection. Confirm it is actually deleted.
6. Remove collection-specific encryption, network and data-access policies if they remain and are not shared.
7. Drop the Athena view and table; delete the project Glue database and Athena workgroup.
8. Empty the dedicated S3 bucket, including query results, sessions and code; then delete it.
9. Delete project-only CloudWatch log groups.
10. Delete the two manually created IAM roles and their inline policies. Remove the Bedrock-created role only if it is not used elsewhere.
11. Verify the resource lists and billing console.

If you enabled S3 versioning, empty noncurrent versions and delete markers as well. Do not delete shared account resources.

## 22. Completion checklist

Use this checklist to confirm the outcomes from the preceding steps. Complete cleanup when you no longer need the deployment.

| Area | Expected outcome |
|---|---|
| Project scope, source data and architecture | You have reviewed the source provenance, supported question types and responsibilities of each AWS service. |
| Data model, Glue processing and Athena | Ingestion reaches `READY`, and the current-view counts match the processed batch. |
| OpenSearch ingestion and retrieval | Documents and metadata are retrievable; hybrid search and reranking return relevant passages. |
| Tool calls and deployed agent | The AgentCore path returns answers with the expected tools, sources and freshness information. |
| Incremental changes | New arrivals, duplicate replay, corrections and deletion produce the expected counts and document versions. |
| Recovery and cleanup | If you ran the optional failure check, recovery restored `READY`; resources you no longer need have been removed using step 21. |

Retain a record of your resource names and any deployment-specific changes so you can reproduce the setup.

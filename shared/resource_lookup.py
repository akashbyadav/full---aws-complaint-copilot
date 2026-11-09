"""Find AWS-assigned identifiers using the fixed names in config/config.json.

Learners create resources through the console using the supplied names. AWS
assigns knowledge-base IDs, data-source IDs and runtime ARNs during creation;
these values cannot be selected beforehand. These small read-only lookups keep
those generated identifiers out of the setup files and prebuilt deployment ZIP.

Nothing runs on import. Each lookup uses the caller's existing AWS credentials
and the configured region. Missing or duplicate names cause an explicit error;
we never silently select an unrelated resource or create a replacement.
"""

from shared.aws_helpers import client


def find_named_resource(list_operation, result_key, name_key, name, **parameters):
    """Return exactly one matching resource, checking every response page.

    Args:
        list_operation: A bound boto3 list method, such as list_knowledge_bases.
        result_key: The response field containing the list of resources.
        name_key: The resource field containing its console name.
        name: The exact name already supplied in the project configuration.
        parameters: Required API inputs, such as the parent knowledgeBaseId.

    Pagination matters even for a small project: the AWS account may contain
    other projects, and our resource may not be on the first page of results.
    """
    matches = []

    while True:
        response = list_operation(**parameters)

        for resource in response.get(result_key, []):
            if resource[name_key] == name:
                matches.append(resource)

        next_token = response.get("nextToken")

        if not next_token:
            break

        parameters["nextToken"] = next_token

    if len(matches) != 1:
        raise ValueError(
            "Expected exactly one AWS resource named "
            + repr(name)
            + "; found "
            + str(len(matches))
            + ". Check the account, us-east-1 region, and names in SETUP_AND_RUN.md."
        )

    return matches[0]


def knowledge_base_id(config):
    """Resolve the knowledge base once for this in-memory configuration.

    Both ingestion and retrieval need this ID. SQL-only turns do not, so the
    agent calls this function only when it actually searches the knowledge base.
    The cached ID is not written back to config/config.json.
    """
    if not config.get("knowledge_base_id"):
        bedrock = client("bedrock-agent", config)
        resource = find_named_resource(
            bedrock.list_knowledge_bases,
            "knowledgeBaseSummaries",
            "name",
            config["knowledge_base_name"],
        )
        config["knowledge_base_id"] = resource["knowledgeBaseId"]

    return config["knowledge_base_id"]


def data_source_id(config):
    """Find the named S3 source inside our KB; only ingestion needs this lookup.

    A supplied ID is accepted for the optional deliberate-failure demonstration.
    Normal setup uses only data_source_name from the delivered configuration.
    """
    if not config.get("data_source_id"):
        bedrock = client("bedrock-agent", config)
        resource = find_named_resource(
            bedrock.list_data_sources,
            "dataSourceSummaries",
            "name",
            config["data_source_name"],
            knowledgeBaseId=knowledge_base_id(config),
        )
        config["data_source_id"] = resource["dataSourceId"]

    return config["data_source_id"]


def agent_runtime_arn(config):
    """Find the deployed runtime for the local Streamlit caller.

    Only the UI calls this function. The deployed agent does not need to find
    itself, so its execution role does not receive ListAgentRuntimes permission.
    """
    if not config.get("agent_runtime_arn"):
        control = client("bedrock-agentcore-control", config)
        resource = find_named_resource(
            control.list_agent_runtimes,
            "agentRuntimes",
            "agentRuntimeName",
            config["agent_runtime_name"],
        )

        if resource["status"] != "READY":
            raise RuntimeError(
                "The AgentCore runtime is not ready: " + resource["status"]
            )

        config["agent_runtime_arn"] = resource["agentRuntimeArn"]

    return config["agent_runtime_arn"]

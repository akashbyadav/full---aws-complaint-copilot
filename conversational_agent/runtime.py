"""Launch the same conversation handler as an AgentCore HTTP application.

This small file contains only hosting logic. Conversation behavior remains in
agent.py, which is also used by the local chat interface.

For a local server, run from the project root:
    python -m conversational_agent.runtime

The deployment helper generates a tiny launcher in the ZIP root that calls
main(). That keeps package imports identical on the laptop and in AgentCore.
"""

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from conversational_agent.agent import handle_request


def main():
    """Register the request handler and start AgentCore's HTTP server.

    The SDK supplies the invocation and health endpoints required by AWS.
    Registration itself does not call Bedrock; services are used when a real
    request reaches handle_request().
    """
    application = BedrockAgentCoreApp()
    application.entrypoint(handle_request)
    application.run()


if __name__ == "__main__":
    main()

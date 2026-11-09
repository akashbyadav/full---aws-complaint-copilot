"""Streamlit chat interface for the local or AWS-hosted conversational agent.

Run from the project root:
    python -m streamlit run app/chat.py

Streamlit re-executes this file after user interactions. session_state therefore
stores the displayed conversation and its UUID between those executions.
"""

import json
import uuid

import streamlit as st

from conversational_agent.agent import handle_request
from shared.aws_helpers import client, load_config
from shared.resource_lookup import agent_runtime_arn


# 1. Configure the page before drawing any widgets.
st.set_page_config(
    page_title="Complaint Intelligence Copilot",
    layout="wide",
)
st.title("Complaint Intelligence Copilot")
st.caption(
    "Explore a historical sample with SQL, cited evidence, and official guidance."
)

# A missing configuration is a setup problem. Display the actionable message
# and stop this page execution before starting a conversation. Loading the
# config resolves the bucket account using STS and the existing AWS identity.
try:
    config = load_config()
except Exception as error:
    st.error(str(error))
    st.stop()


# 2. Create a conversation once, rather than on every Streamlit rerun.
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.turns = []

mode = st.sidebar.radio(
    "Agent execution",
    ["AWS AgentCore", "Local Python"],
    index=0,
)
st.sidebar.caption(
    "Local mode still uses AWS models, SQL, retrieval, and session storage."
)

if st.sidebar.button("New conversation"):
    # A new UUID selects a new S3 session. Existing saved sessions are not erased.
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.turns = []
    st.rerun()


# 3. Redraw completed turns. Keep the detailed trace beside the answer so a
# student can inspect actual SQL, retrieval passages, tool errors and freshness.
for turn in st.session_state.turns:
    with st.chat_message(turn["role"]):
        st.markdown(turn["text"])

        if "result" in turn:
            with st.expander("Sources, SQL, tools, and freshness"):
                st.json(turn["result"])


# 4. A new question invokes exactly one complete agent turn. Local execution
# calls the Python handler; remote execution sends the same payload to AWS.
question = st.chat_input("Ask about counts, reported experiences, or official guidance")

if question:
    st.session_state.turns.append(
        {
            "role": "user",
            "text": question,
        }
    )

    with st.chat_message("user"):
        st.markdown(question)

    payload = {
        "prompt": question,
        "session_id": st.session_state.session_id,
    }

    try:
        with st.spinner("Gathering evidence..."):
            if mode == "AWS AgentCore":
                # AWS assigns the ARN during creation. Find the fixed runtime
                # name here so learners never have to edit it into a config.
                response = client("bedrock-agentcore", config).invoke_agent_runtime(
                    agentRuntimeArn=agent_runtime_arn(config),
                    runtimeSessionId=st.session_state.session_id,
                    payload=json.dumps(payload).encode("utf-8"),
                    contentType="application/json",
                )

                # The SDK returns a streaming body. Our runtime returns one JSON
                # dictionary, so read and decode it before accessing the answer.
                result = json.loads(response["response"].read())
            else:
                result = handle_request(payload)

        st.session_state.turns.append(
            {
                "role": "assistant",
                "text": result["answer"],
                "result": result,
            }
        )
        st.rerun()

    except Exception as error:
        st.error(str(error))

        # Remove the pending question from the displayed completed-turn list.
        # Remote request failures can be ambiguous: inspect runtime logs before
        # assuming a timed-out request did not finish on the server.
        st.session_state.turns.pop()

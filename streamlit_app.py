"""Step 6 - Streamlit UI.  Run: streamlit run streamlit_app.py

Privacy: chat history lives only in this browser session. Messages containing
personal information are hidden in the transcript and never sent to the LLM.
"""
import os

import streamlit as st

from app.guardrails import detect_pii
from app.pipeline import ask
from app.retriever import Retriever

# Streamlit Cloud secrets -> environment (llm.py reads env vars)
try:
    for key in ("GEMINI_API_KEY", "GEMINI_MODEL"):
        if key in st.secrets and key not in os.environ:
            os.environ[key] = st.secrets[key]
except Exception:
    pass  # no secrets file locally: use exported env vars

DISCLAIMER = (
    "Facts-only. No investment advice. Answers come from official ICICI Prudential "
    "Mutual Fund, SEBI and AMFI pages, with a source link on every answer. "
    "Please don't share PAN, Aadhaar, OTPs, account numbers, phone numbers or email addresses."
)
EXAMPLES = [
    "What is the expense ratio of ICICI Prudential Flexicap Fund?",
    "What is the lock-in period of the ELSS Tax Saver Fund?",
    "What is the exit load of ICICI Prudential Large Cap Fund?",
]
SCOPE = "Covers ICICI Prudential Large Cap (formerly Bluechip), Flexi Cap and ELSS Tax Saver funds."

st.set_page_config(page_title="Mutual fund facts", page_icon="📘", layout="centered")
st.markdown(
    """<style>
    .block-container {max-width: 760px; padding-top: 2.2rem;}
    .disclaimer {background:#F1F6F5; border-left:3px solid #0F7B6C; padding:.6rem .9rem;
                 border-radius:4px; font-size:.9rem; margin:.4rem 0 1rem;}
    .scope {color:#5B6B69; font-size:.85rem; margin-bottom:.6rem;}
    </style>""",
    unsafe_allow_html=True,
)


@st.cache_resource(show_spinner="Loading official sources…")
def get_retriever() -> Retriever:
    return Retriever(use_embeddings=os.getenv("USE_EMBEDDINGS", "1") == "1")


st.title("Mutual fund facts, straight from the source")
st.write("Ask about expense ratio, exit load, minimum SIP, lock-in, riskometer, "
         "or benchmark for three ICICI Prudential schemes on Groww.")
st.markdown(f'<div class="disclaimer">{DISCLAIMER}</div>', unsafe_allow_html=True)
st.markdown(f'<div class="scope">{SCOPE}</div>', unsafe_allow_html=True)

if "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.route_state = {}

clicked = None
if not st.session_state.messages:
    st.write("Try one of these:")
    cols = st.columns(3)
    for col, q in zip(cols, EXAMPLES):
        if col.button(q, use_container_width=True):
            clicked = q

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

question = st.chat_input("Ask a factual question about these funds") or clicked
if question:
    shown = ("*Message hidden because it contained personal information.*"
             if detect_pii(question) else question)
    st.session_state.messages.append({"role": "user", "content": shown})
    with st.chat_message("user"):
        st.markdown(shown)

    with st.chat_message("assistant"):
        with st.spinner("Checking official sources…"):
            try:
                result = ask(question, st.session_state.route_state, get_retriever())
                text = result["text"].replace("\n", "  \n")  # keep line breaks in markdown
            except FileNotFoundError:
                text = "The source index is missing. Run `python ingest.py` first, then restart the app."
            except Exception:
                text = ("Something went wrong while answering. Check that GEMINI_API_KEY is set, "
                        "then try again.")
        st.markdown(text)
    st.session_state.messages.append({"role": "assistant", "content": text})
    if clicked:
        st.rerun()

if st.session_state.messages and st.button("Clear chat"):
    st.session_state.messages, st.session_state.route_state = [], {}
    st.rerun()

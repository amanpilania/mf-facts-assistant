"""Chat with the assistant in the terminal: python cli.py  (add --debug to see routing)"""
import sys

from app.pipeline import ask
from app.retriever import Retriever

WELCOME = """Facts-Only MF Assistant (ICICI Prudential schemes on Groww)
Facts-only. No investment advice.
Try: "Expense ratio of ICICI Prudential Flexicap Fund?" | "ELSS lock-in period?" |
     "Exit load of ICICI Bluechip?"   (type 'quit' to exit)"""

def main():
    debug = "--debug" in sys.argv
    retriever, state = Retriever(), {}
    print(WELCOME)
    while True:
        q = input("\nYou: ").strip()
        if q.lower() in {"quit", "exit"}:
            break
        if not q:
            continue
        r = ask(q, state, retriever)
        print("\nAssistant:", r["text"])
        if debug:
            print({k: v for k, v in r.items() if k not in ("text", "question")})

if __name__ == "__main__":
    main()

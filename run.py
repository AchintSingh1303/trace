"""Start TRACE: python run.py [--port 8000] [--host 127.0.0.1] [--open]"""

import argparse
import threading
import webbrowser

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TRACE demo server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="open the app in your browser")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  TRACE is starting at {url}")
    print("  Demo login: lead@demo.local / demo123   (also admin@, tester@, viewer@)\n")
    if args.open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

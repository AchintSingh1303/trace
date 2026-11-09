"""Score a generator against eval/golden.json.

    python scripts/run_eval.py                       # built-in rules, offline
    python scripts/run_eval.py --provider anthropic  # needs LLM_API_KEY in the environment
    python scripts/run_eval.py --provider openai --model gpt-4o --strict

Exits with status 1 when a threshold in the golden file is missed, so it can gate a CI job.
The key is read from the environment only and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.evaluation import llm_generator, rules_generator, score  # noqa: E402
from app.services.llm import PROVIDERS, LLMConfig  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["rules", "anthropic", "openai"], default="rules")
    ap.add_argument("--model")
    ap.add_argument("--golden", default=str(ROOT / "eval" / "golden.json"))
    ap.add_argument("--strict", action="store_true", help="also drop cases that mention terms not in the requirement")
    ap.add_argument("--json", action="store_true", help="print the raw result as JSON")
    args = ap.parse_args()

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    if args.provider == "rules":
        generator = rules_generator
    else:
        key = os.getenv("LLM_API_KEY", "").strip()
        if not key:
            print("Set LLM_API_KEY in the environment to evaluate a model.", file=sys.stderr)
            return 2
        model = args.model or PROVIDERS[args.provider]["default_model"]
        generator = llm_generator(LLMConfig(args.provider, key, model), args.strict)

    result = score(golden, generator)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Generator: {args.provider}   cases: {result['cases']}")
        print(f"{'Requirement':<12}{'cases':>6}{'criteria':>10}{'must-cover':>12}  invented")
        for r in result["requirements"]:
            crit = f"{r['criteria_covered']}/{r['criteria_total']}"
            must = f"{r['must_hit']}/{r['must_total']}"
            print(f"{r['code']:<12}{r['cases']:>6}{crit:>10}{must:>12}  {', '.join(r['forbidden_hits']) or '-'}")
        print(
            f"\ncriteria coverage {result['criteria_coverage']:.0%}   must-cover {result['must_cover']:.0%}   "
            f"invented phrases {result['forbidden_hits']}   dropped {result['dropped']}   "
            f"ungrounded rate {result['ungrounded_rate']:.0%}"
        )
        print("PASS" if result["passed"] else "FAIL: " + "; ".join(result["failures"]))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

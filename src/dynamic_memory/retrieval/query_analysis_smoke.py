"""Inspect query interpretation without an API, model or dataset."""

import argparse
from datetime import date

from dynamic_memory.contracts.models import QueryRequest

from .query_analysis import QUERY_ANALYSIS_VERSION, QueryAnalyzer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default="Where does Sam work now?")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--entity", action="append", default=[], help="Known entity name; repeat as needed")
    args = parser.parse_args()
    result = QueryAnalyzer(args.entity).analyze(QueryRequest(
        query_id="synthetic_query_demo", sample_id="synthetic_001",
        question=args.question, as_of=args.as_of,
    ))
    print(QUERY_ANALYSIS_VERSION)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

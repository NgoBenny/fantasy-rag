import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fantasy-rag")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-sources", help="sample every core source, write docs/data_sources.md")
    ingest = sub.add_parser("ingest", help="load data into Postgres")
    ingest.add_argument("what", choices=["structured"])
    args = parser.parse_args(argv)

    if args.command == "verify-sources":
        from fantasy_rag.verify_sources import run

        print(f"wrote {run()}")
    elif args.command == "ingest":
        from fantasy_rag.db import get_engine
        from fantasy_rag.ingest.structured import run_structured

        report = run_structured(get_engine())
        print(f"{'source':32} {'changed':>9} {'unresolved':>11}  note")
        for r in report.results:
            note = f"FAILED {r.error}" if r.error else r.note
            print(f"{r.source:32} {r.changed:>9} {r.unresolved:>11}  {note}")
        if not report.ok:
            sys.exit(1)

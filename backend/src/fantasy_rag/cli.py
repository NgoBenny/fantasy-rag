import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fantasy-rag")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-sources", help="sample every core source, write docs/data_sources.md")
    args = parser.parse_args(argv)

    if args.command == "verify-sources":
        from fantasy_rag.verify_sources import run

        print(f"wrote {run()}")

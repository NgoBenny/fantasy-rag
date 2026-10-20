import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fantasy-rag")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-sources", help="sample every core source, write docs/data_sources.md")
    ingest = sub.add_parser("ingest", help="load data into Postgres")
    ingest.add_argument("what", choices=["structured"])
    league = sub.add_parser("league", help="Sleeper league import and scoring check")
    league.add_argument("action", choices=["import", "check-scoring"])
    league.add_argument("--username", help="Sleeper username (default: SLEEPER_USERNAME)")
    league.add_argument("--league-id", help="import one league instead of all of a user's")
    league.add_argument(
        "--season", help="with --username: that season's leagues (default: current)"
    )
    tool = sub.add_parser(
        "tool", help="run one agent tool by hand, e.g. tool project_player '{...}'"
    )
    tool.add_argument("name")
    tool.add_argument("args", nargs="?", default="{}", help="JSON arguments")
    bt = sub.add_parser("backtest", help="tune the baseline on one season, test on the next")
    bt.add_argument("--tune-season", type=int, default=2024)
    bt.add_argument("--test-season", type=int, default=2025)
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
    elif args.command == "league":
        _league(args)
    elif args.command == "tool":
        _tool(args.name, args.args)
    elif args.command == "backtest":
        _backtest(args.tune_season, args.test_season)


def _tool(name: str, raw_args: str) -> None:
    import json

    from fantasy_rag.agent.tools import run_tool
    from fantasy_rag.db import get_readonly_engine

    with get_readonly_engine().connect() as conn:
        print(json.dumps(run_tool(conn, name, json.loads(raw_args)), indent=2, default=str))


def _backtest(tune: int, test: int) -> None:
    from fantasy_rag.db import get_engine
    from fantasy_rag.ml.tune import run_backtest

    r = run_backtest(get_engine(), tune, test)
    b, t = r["best"], r["test"]
    print(
        f"tuned on {tune}: half_life={b['half_life']} prior_season={b['use_prior_season']}"
        f" MAE={b['mae']} start/sit={b['start_sit_accuracy']}"
    )
    print(
        f"test {test}: MAE={t['mae']} by position={t['mae_by_position']}"
        f" start/sit={t['start_sit_accuracy']} ({t['start_sit_pairs']} pairs, n={t['n']})"
    )
    for name, v in r["naive"].items():
        print(f"  vs {name}: MAE={v['mae']} start/sit={v['start_sit_accuracy']}")


def _league(args: argparse.Namespace) -> None:
    from sqlalchemy import text

    from fantasy_rag.config import Settings
    from fantasy_rag.db import get_engine

    engine = get_engine()
    if args.action == "import":
        from fantasy_rag.ingest.league import import_leagues
        from fantasy_rag.ingest.sleeper import Sleeper

        username = args.username or Settings().sleeper_username
        if not (username or args.league_id):
            sys.exit("give --username, --league-id, or set SLEEPER_USERNAME in .env")
        for s in import_leagues(engine, Sleeper(), username, args.league_id, args.season):
            print(
                f"league {s['league_pk']}: {s['rosters']} rosters x {s['weeks']} weeks,"
                f" {s['changed']} rows changed, {s['unresolved']} unresolved players,"
                f" unsupported scoring keys: {', '.join(s['unsupported_scoring']) or 'none'}"
            )
        return

    from fantasy_rag.analytics.scoring_check import check_league

    failed = False
    with engine.connect() as conn:
        pks = conn.execute(text("SELECT id FROM fantasy_leagues ORDER BY id")).scalars().all()
        for pk in pks:
            r = check_league(conn, pk)
            span = f"{r.weeks[0]}-{r.weeks[-1]}" if r.weeks else "none"
            print(
                f"league {pk}: weeks {span},"
                f" {r.within}/{r.compared} player-weeks within 0.1"
                f" (skipped: {r.skipped_def} team DEF, {r.skipped_unresolved} unresolved)"
            )
            for m in sorted(r.mismatches, key=lambda m: -abs(m.ours - m.sleeper))[:10]:
                print(
                    f"   wk{m.week} {m.name} ({m.position}) sleeper={m.sleeper} ours={m.ours}"
                    f" diff={m.ours - m.sleeper:+.2f} {m.breakdown}"
                    f" unscored: {', '.join(m.unscored) or 'none'}"
                )
            failed |= bool(r.mismatches)
    if failed:
        sys.exit(1)

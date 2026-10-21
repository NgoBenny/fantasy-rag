import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fantasy-rag")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-sources", help="sample every core source, write docs/data_sources.md")
    ingest = sub.add_parser("ingest", help="load data into Postgres")
    ingest.add_argument("what", choices=["structured", "news", "news-report"])
    league = sub.add_parser("league", help="Sleeper league import and scoring check")
    league.add_argument("action", choices=["import", "check-scoring", "sync-transactions"])
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
    ask = sub.add_parser("ask", help="answer a question with cited tool results (Claude API)")
    ask.add_argument("question")
    ask.add_argument("--league-id", type=int, help="the league the question is about")
    dm = sub.add_parser("demo", help="build data/demo/demo.json for DEMO_MODE (free by default)")
    dm.add_argument("action", choices=["record"])
    dm.add_argument("--report", required=True, help="saved eval_answers report to reuse")
    ev = sub.add_parser("eval", help="evaluate retrieval on the labelled questions")
    ev.add_argument("what", choices=["news", "answers", "grounding"])
    ev.add_argument("--only", help="answers: comma-separated question ids or sets")
    ev.add_argument("--rescore", help="answers: grade a saved report again (no API calls)")
    ev.add_argument("--report", help="grounding: saved full answer report (paid review only)")
    ev.add_argument("--rerank", action="store_true", help="also score hybrid + reranker")
    args = parser.parse_args(argv)
    if args.command == "eval" and args.what == "grounding" and not args.report:
        parser.error("eval grounding requires --report; it never regenerates answers")

    if args.command == "verify-sources":
        from fantasy_rag.verify_sources import run

        print(f"wrote {run()}")
    elif args.command == "ingest":
        from fantasy_rag.db import get_engine
        from fantasy_rag.ingest.structured import run_structured

        if args.what == "news-report":
            import json

            from fantasy_rag.ingest.news_job import coverage

            with get_engine().connect() as conn:
                print(json.dumps(coverage(conn), indent=2, default=str))
            return
        if args.what == "news":
            from fantasy_rag.index.embedder import LocalBGE
            from fantasy_rag.ingest.news_job import run_news

            nr = run_news(get_engine(), LocalBGE())
            print(
                f"stories {nr.stories}, documents changed {nr.documents_changed},"
                f" chunks written {nr.chunks_written}, embedded {nr.embedded},"
                f" ambiguous names {len(set(nr.ambiguous_names))},"
                f" feed errors {len(nr.feed_errors)}"
            )
            for e in nr.feed_errors:
                print(f"  feed error: {e}")
            return
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
    elif args.command == "demo":
        from pathlib import Path

        from fantasy_rag.db import get_readonly_engine
        from fantasy_rag.demo_record import build, write

        with get_readonly_engine().connect() as conn:
            print(f"wrote {write(build(conn, Path(args.report), []))}")
    elif args.command == "ask":
        _ask(args.question, args.league_id)
    elif args.command == "eval" and args.what == "answers":
        _eval_answers(args.only, args.rescore)
    elif args.command == "eval" and args.what == "grounding":
        _eval_grounding(args.report, args.only)
    elif args.command == "eval":
        import json

        from fantasy_rag.db import get_readonly_engine
        from fantasy_rag.index.embedder import LocalBGE
        from fantasy_rag.retrieval.evaluate import evaluate, evaluate_heldout
        from fantasy_rag.retrieval.rerank import LocalBGEReranker

        with get_readonly_engine().connect() as conn:
            rr = LocalBGEReranker() if args.rerank else None
            print(json.dumps(evaluate(conn, LocalBGE(), reranker=rr), indent=2))
            print(json.dumps({"heldout": evaluate_heldout(conn)}, indent=2, default=str))
            if rr is not None:
                held = evaluate_heldout(conn, reranker=rr)
                print(json.dumps({"heldout+rerank": held}, indent=2, default=str))


def _tool(name: str, raw_args: str) -> None:
    import json

    from fantasy_rag.agent.tools import run_tool
    from fantasy_rag.db import get_readonly_engine

    with get_readonly_engine().connect() as conn:
        print(json.dumps(run_tool(conn, name, json.loads(raw_args)), indent=2, default=str))


def _ask(question: str, league_id: int | None) -> None:
    import json

    from fantasy_rag.agent.llm import AnthropicLLM
    from fantasy_rag.agent.router import ask, my_leagues
    from fantasy_rag.config import Settings
    from fantasy_rag.db import get_readonly_engine

    llm = AnthropicLLM()
    with get_readonly_engine().connect() as conn:
        turn = ask(conn, llm, question, my_leagues(conn, Settings().sleeper_username), league_id)
    print(turn.answer.answer)
    print(json.dumps(turn.answer.model_dump(exclude={"answer"}), indent=2))
    print(f"tools: {[(c.name, c.args, c.error) for c in turn.calls]}")
    print(
        f"first draft problems: {turn.first_problems or 'none'};"
        f" final: {turn.problems or 'none'}; {turn.latency_ms} ms, ${llm.cost_usd():.4f}"
    )


def _eval_answers(only: str | None, rescore_path: str | None) -> None:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from fantasy_rag.agent.evaluate import rescore, run_golden
    from fantasy_rag.agent.llm import AnthropicLLM
    from fantasy_rag.config import REPO_ROOT, Settings
    from fantasy_rag.db import get_readonly_engine

    with get_readonly_engine().connect() as conn:
        if rescore_path:
            saved = json.loads(Path(rescore_path).read_text(encoding="utf-8"))
            report = rescore(conn, saved, Settings().sleeper_username)
        else:
            ids = set(only.split(",")) if only else None
            report = run_golden(conn, AnthropicLLM(), Settings().sleeper_username, ids)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    kind = "rescored" if rescore_path else "answers"
    path = REPO_ROOT / "data" / "logs" / f"eval_{kind}_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"full report: {path}")


def _eval_grounding(report_path: str, only: str | None) -> None:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from fantasy_rag.agent.grounding import audit_saved
    from fantasy_rag.agent.llm import AnthropicLLM
    from fantasy_rag.config import REPO_ROOT

    saved = json.loads(Path(report_path).read_text(encoding="utf-8-sig"))
    llm = AnthropicLLM()
    ids = {s.strip() for s in only.split(",")} if only else None
    report = audit_saved(saved, llm, ids)
    report["source_report"] = str(Path(report_path).resolve())
    report["summary"] |= {"tokens": llm.usage, "cost_usd": round(llm.cost_usd(), 4)}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPO_ROOT / "data" / "logs" / f"eval_grounding_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = report["summary"]
    print(json.dumps(summary, indent=2))
    print(f"full audit: {path}")
    if (
        any(
            summary[k]
            for k in (
                "unsupported_answers",
                "missing_requirements",
                "validation_failures",
                "review_errors",
            )
        )
        or (summary["mean_groundedness"] or 0) < 4.5
    ):
        sys.exit(1)


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
    if args.action == "sync-transactions":
        from fantasy_rag.ingest.sleeper import Sleeper
        from fantasy_rag.ingest.transactions import sync_transactions

        failed = False
        for s in sync_transactions(engine, Sleeper()):
            print(f"league {s['league_pk']}: weeks {s['weeks']}, failed: {s['failed'] or 'none'}")
            failed |= bool(s["failed"])
        if failed:
            sys.exit(1)
        return
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
                f" (team DEF {r.def_within}/{r.def_compared};"
                f" skipped: {r.skipped_unresolved} unresolved)"
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

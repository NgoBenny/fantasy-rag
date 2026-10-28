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
    bk = sub.add_parser("backup", help="verified local dump outside the repo (+ roles, manifest)")
    bk.add_argument(
        "--verify-latest",
        action="store_true",
        help="restore the newest dump into a fresh container and check it",
    )
    sub.add_parser("forward-check", help="exit 1 if this week's on-time forward run is missing")
    sub.add_parser(
        "usage-check", help="Phase 10: play-by-play usage vs nflverse stats (exit 1 on miss)"
    )
    sub.add_parser("calibration-seed", help="Phase 8: record the frozen range calibration, once")
    sub.add_parser("predict", help="Phase 8 forward run: log baseline + TE Ridge (Tuesdays)")
    bm = sub.add_parser("benchmark", help="Phase 8: baseline vs Ridge vs LightGBM, frozen rules")
    bm.add_argument("--predictions", help="re-report a saved predictions file (no retraining)")
    ask = sub.add_parser("ask", help="answer a question with cited tool results (Claude API)")
    ask.add_argument("question")
    ask.add_argument("--league-id", type=int, help="the league the question is about")
    dm = sub.add_parser("demo", help="build data/demo/demo.json for DEMO_MODE (free by default)")
    dm.add_argument("action", choices=["record"])
    dm.add_argument("--report", required=True, help="saved eval_answers report to reuse")
    dm.add_argument("--record-question", help="ask one league-free question live (paid)")
    dm.add_argument("--budget-usd", type=float, help="spending cap; required to record")
    ev = sub.add_parser("eval", help="evaluate retrieval on the labelled questions")
    ev.add_argument("what", choices=["news", "answers", "grounding"])
    ev.add_argument("--only", help="answers: comma-separated question ids or sets")
    ev.add_argument("--rescore", help="answers: grade a saved report again (no API calls)")
    ev.add_argument("--report", help="grounding: saved full answer report (paid review only)")
    ev.add_argument("--rerank", action="store_true", help="also score hybrid + reranker")
    args = parser.parse_args(argv)
    if args.command == "eval" and args.what == "grounding" and not args.report:
        parser.error("eval grounding requires --report; it never regenerates answers")
    lock = _lock_for(args)
    if lock is None:
        _dispatch(args)
        return
    from fantasy_rag.db import get_engine
    from fantasy_rag.jobs import locks
    from fantasy_rag.jobs.locks import job_lock

    wait = locks.PREDICT_WAIT if args.command == "predict" else 0
    with job_lock(get_engine(), lock, wait) as owned:
        if not owned and wait:
            print(f"{args.command}: lock {lock} still held after {wait} s; nothing written")
            sys.exit(1)  # the week's prediction is at stake: fail so the task's toast fires
        if not owned:
            print(f"{args.command}: already running (lock {lock}); nothing to do")
            return
        _dispatch(args)


def _lock_for(args: argparse.Namespace) -> int | None:
    """The one lock a command takes (jobs.locks); read-only commands take none."""
    from fantasy_rag.jobs import locks

    what = getattr(args, "what", None) or getattr(args, "action", None)
    return {
        ("ingest", "structured"): locks.STRUCTURED,
        ("ingest", "news"): locks.NEWS,
        ("league", "import"): locks.STRUCTURED,
        ("league", "sync-transactions"): locks.TRANSACTIONS,
        ("predict", None): locks.STRUCTURED,
        ("calibration-seed", None): locks.STRUCTURED,
        ("backup", None): locks.BACKUP,
    }.get((args.command, what if args.command in ("ingest", "league") else None))


def _dispatch(args: argparse.Namespace) -> None:
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
    elif args.command == "backup":
        from fantasy_rag.jobs import backup

        if args.verify_latest:
            drill = backup.verify_latest()
            print(f"restored {drill['dump']} into a fresh container in {drill['seconds']} s;"
                  f" {drill['tables']} tables checked")  # fmt: skip
            for p in drill["problems"]:
                print(f"  PROBLEM: {p}")
            if drill["problems"]:
                sys.exit(1)
        else:
            print(f"backup written: {backup.run()}")
    elif args.command == "usage-check":
        _usage_check()
    elif args.command == "forward-check":
        from datetime import UTC, datetime

        from fantasy_rag.db import get_engine
        from fantasy_rag.ml.forward_results import check

        with get_engine().connect() as conn:
            ok, message = check(conn, datetime.now(UTC))
        print(message)
        if not ok:
            sys.exit(1)
    elif args.command == "calibration-seed":
        from fantasy_rag.db import get_engine
        from fantasy_rag.ml.predict import seed

        print(f"recorded {seed(get_engine())} calibration errors (seed; never rebuilt)")
    elif args.command == "predict":
        _predict()
    elif args.command == "benchmark":
        _benchmark(args.predictions)
    elif args.command == "demo":
        from pathlib import Path

        from fantasy_rag.db import get_readonly_engine
        from fantasy_rag.demo_record import build, load_recorded, write

        if args.record_question:
            _record_demo(args.record_question, args.budget_usd)
        with get_readonly_engine().connect() as conn:
            print(f"wrote {write(build(conn, Path(args.report), load_recorded()))}")
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


def _record_demo(question: str, cap: float | None) -> None:
    """Paid: one live answer under a hard cap; saved only if it passed every check."""
    from fantasy_rag.agent.budget import Budget
    from fantasy_rag.agent.llm import make_llm
    from fantasy_rag.db import get_readonly_engine
    from fantasy_rag.demo_record import record_question, save_recorded

    if cap is None:
        raise SystemExit("--record-question spends money: pass --budget-usd")
    budget = Budget(cap)
    llm = make_llm(budget)
    try:
        with get_readonly_engine().connect() as conn:
            chat = record_question(conn, llm, question)
    finally:  # report what was spent even when recording fails
        print(f"spent ${budget.spent:.4f} of ${cap:.2f} (calls with unknown usage: "
              f"{llm.unpriced}); tokens {llm.usage}")  # fmt: skip
    save_recorded(chat)
    done = next(e for e in chat["events"] if e["event"] == "done")["data"]
    print(f"recorded: first progress {done['progress_first_ms']} ms, answer {done['answer_ms']} ms")


def _ask(question: str, league_id: int | None) -> None:
    import json

    from fantasy_rag.agent.llm import make_llm
    from fantasy_rag.agent.router import ask, my_leagues
    from fantasy_rag.config import Settings
    from fantasy_rag.db import get_readonly_engine

    llm = make_llm()
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
    from fantasy_rag.agent.llm import make_llm
    from fantasy_rag.config import REPO_ROOT, Settings
    from fantasy_rag.db import get_readonly_engine

    with get_readonly_engine().connect() as conn:
        if rescore_path:
            saved = json.loads(Path(rescore_path).read_text(encoding="utf-8"))
            report = rescore(conn, saved, Settings().sleeper_username)
        else:
            ids = set(only.split(",")) if only else None
            report = run_golden(conn, make_llm(), Settings().sleeper_username, ids)
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
    from fantasy_rag.agent.llm import make_llm
    from fantasy_rag.config import REPO_ROOT

    saved = json.loads(Path(report_path).read_text(encoding="utf-8-sig"))
    llm = make_llm()
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


def _predict() -> None:
    from fantasy_rag.db import get_engine
    from fantasy_rag.ml.predict import run

    r = run(get_engine())
    if "skipped" in r:
        print(f"predict: {r['skipped']}; nothing written")
        return
    print(
        f"forward run {r['run_id']}: {r['season']} week {r['week']} {r['status'].upper()}"
        f" (data as of {r['snapshot_at']:%Y-%m-%d %H:%M %Z},"
        f" deadline {r['cutoff']:%Y-%m-%d %H:%M %Z},"
        f" newest game {r['newest_game']}); {r['players']} players"
    )
    print(f"models: {r['models']}")
    print(f"calibration errors: {r['calibration_errors']} ({r['finalized_errors']} added from"
          " finalized forward weeks)")  # fmt: skip
    if not r["calibration_errors"]:
        print("no calibration errors recorded: no ranges (run `fantasy-rag calibration-seed`)")


def _benchmark(saved: str | None) -> None:
    import json
    import pickle
    from datetime import UTC, datetime
    from pathlib import Path

    from fantasy_rag.config import REPO_ROOT
    from fantasy_rag.db import get_engine
    from fantasy_rag.ml.benchmark import predict_all, report, write_runs

    logs = REPO_ROOT / "data" / "logs"
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    log = lambda m: print(m, flush=True)  # noqa: E731
    if saved:  # our own file from an earlier run (pickle: never load one from elsewhere)
        rows, preds, params = pickle.loads(Path(saved).read_bytes())
    else:
        rows, preds, params = predict_all(get_engine(), log)
        kept = logs / f"benchmark_predictions_{stamp}.pkl"
        kept.write_bytes(pickle.dumps((rows, preds, params)))
        print(f"predictions: {kept}")
    result = report(get_engine(), rows, preds, params, log)
    out = logs / f"benchmark_{stamp}.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    write_runs(get_engine(), result)
    print(f"promotion (frozen rule): {result['promotion']}")
    print(f"report: {out}")


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


def _usage_check() -> None:
    import json
    from datetime import UTC, datetime

    from fantasy_rag.analytics.queries import latest_week
    from fantasy_rag.analytics.usage_check import reconcile
    from fantasy_rag.config import REPO_ROOT
    from fantasy_rag.db import get_engine

    with get_engine().connect() as conn:
        season, _ = latest_week(conn)
        report = reconcile(conn, list(range(2022, season + 1)))
    path = REPO_ROOT / "data" / "logs" / f"usage_check_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, default=str))
    for c in report["cells"]:
        mark = "ok  " if c["ok"] else "FAIL"
        print(
            f"{mark} {c['stat']:8} {c['position']} {c['season']}: {c['exact_pct']:6}% exact"
            f" of {c['rows']} (pbp more {c['pbp_more']}, less {c['pbp_less']}; bar {c['bar']}%)"
        )
    print(
        f"uncovered player-weeks {report['uncovered_player_weeks']}, pbp rows without stats"
        f" {report['pbp_rows_without_stats']}, unresolved pbp IDs {report['unresolved_pbp_ids']}"
    )
    print(f"snap denominators: {report['snap_denominators']}")
    print(f"report: {path}")
    if report["failed"]:
        print("FAILED: " + "; ".join(report["failed"]))
        sys.exit(1)

from datetime import UTC, datetime

from fantasy_rag.index.chunker import chunk
from fantasy_rag.ingest.entity_link import Linker, Player, norm
from fantasy_rag.ingest.news import _when, clean, normalize_url, parse_espn, parse_rss

RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><item>
  <title>Rams put Davante Adams on IR</title>
  <link>https://www.example.com/news/adams-ir/?utm_source=rss#top</link>
  <pubDate>Tue, 20 Oct 2026 18:29:12 -0400</pubDate>
  <description>&lt;p&gt;Wide receiver &lt;b&gt;Davante Adams&lt;/b&gt;
    is expected to miss 6-10 weeks.&lt;/p&gt;</description>
  <content:encoded>FULL ARTICLE TEXT THAT MUST NEVER BE STORED</content:encoded>
</item></channel></rss>"""


def test_rss_keeps_title_summary_link_only() -> None:
    [s] = parse_rss(RSS)
    assert s["url"] == "https://www.example.com/news/adams-ir"
    assert s["summary"] == "Wide receiver Davante Adams is expected to miss 6-10 weeks."
    assert "FULL ARTICLE" not in str(s)
    # "Tue" contains a "T": RSS dates must not be mistaken for ISO 8601.
    assert s["published_at"] == datetime(2026, 10, 20, 22, 29, 12, tzinfo=UTC)


def test_espn_story_carries_athlete_ids_and_team_names() -> None:
    payload = {
        "articles": [
            {
                "headline": "Steelers sign Marquez Valdes-Scantling",
                "description": "Signed on Tuesday.",
                "published": "2026-10-20T22:49:56Z",
                "links": {"web": {"href": "https://www.espn.com/nfl/story/_/id/1/x"}},
                "categories": [
                    {"type": "team", "teamId": 23, "description": "Pittsburgh Steelers"},
                    {"type": "athlete", "athleteId": 3051738, "description": "M. V-S"},
                ],
            },
            {"headline": "no link, skipped"},
        ]
    }
    [s] = parse_espn(payload)
    assert s["espn_athlete_ids"] == ["3051738"] and s["team_names"] == ["Pittsburgh Steelers"]
    assert s["published_at"] == datetime(2026, 10, 20, 22, 49, 56, tzinfo=UTC)


def test_clean_and_url_helpers() -> None:
    assert clean("a &amp; <i>b</i>\n c") == "a & b c"
    assert clean("one two three four", limit=9) == "one two…"
    assert (
        normalize_url("HTTP://Example.com/a/?utm_source=rss&fbclid=x#top")
        == "https://example.com/a"
    )
    # Story-identifying parameters stay, so different stories never collapse into one.
    assert normalize_url("https://x.com/story?id=1&utm_medium=rss") == "https://x.com/story?id=1"
    assert normalize_url("https://x.com/story?id=2") != normalize_url("https://x.com/story?id=1")
    assert _when("not a date") is None


def _linker() -> Linker:
    players = [
        Player("00-1", norm("Tyler Huntley").strip(), "BAL"),
        Player("00-2", norm("Derrick Henry").strip(), "BAL"),
        Player("00-3", norm("Mike Williams").strip(), "NYJ"),
        Player("00-4", norm("Mike Williams").strip(), "WAS"),
        Player("00-5", norm("Amon-Ra St. Brown").strip(), "DET"),
    ]
    return Linker(players, {"999": "00-5"})


def test_back_to_back_names_both_link() -> None:
    ids, _ = _linker().players_in("Tyler Huntley Derrick Henry ran wild", [], [])
    assert ids == ["00-1", "00-2"]


def test_shared_names_need_team_context_and_are_never_guessed() -> None:
    linker = _linker()
    assert linker.players_in("Mike Williams scored", [], []) == ([], ["mike williams"])
    assert linker.players_in("Mike Williams scored", ["WAS"], []) == (["00-4"], [])


def test_espn_ids_teams_and_last_name_follow_ups() -> None:
    linker = _linker()
    assert linker.players_in("No names here", [], ["999"])[0] == ["00-5"]
    assert linker.teams_in("The Lions beat the Detroit Lions?", ["Pittsburgh Steelers"]) == [
        "DET",
        "PIT",
    ]
    assert linker.chunk_players("St. Brown was limited.", ["00-5", "00-2"]) == ["00-5"]
    assert linker.chunk_players("The offense stalled.", ["00-5"]) == []
    assert linker.chunk_players("Brown was limited; Henry rested.", ["00-5", "00-2"]) == [
        "00-5",
        "00-2",
    ]


def test_chunker_splits_long_text_on_sentences() -> None:
    assert chunk("Title", "Short summary.") == ["Title. Short summary"]
    long = " ".join(f"Sentence {i} has five words." for i in range(150))  # 750 words
    pieces = chunk("T", long, max_words=300)
    assert len(pieces) == 3 and all(len(p.split()) <= 300 for p in pieces)


def test_article_text_never_becomes_the_summary() -> None:
    # No <description>: feedparser copies content:encoded into `summary`. Store nothing.
    raw = b"""<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><item><title>T</title><link>https://example.com/s</link>
<content:encoded>FULL ARTICLE TEXT</content:encoded></item></channel></rss>"""
    [s] = parse_rss(raw)
    assert s["summary"] == ""


def test_namesakes_need_teams_that_single_out_one_player() -> None:
    # The Vikings WR is the only fantasy "Justin Jefferson"; a Browns LB shares the name.
    linker = Linker([Player("wr", "justin jefferson", "MIN")], {}, {"justin jefferson": ["CLE"]})
    story = "Justin Jefferson questionable to return"
    assert linker.players_in(story, ["MIN"], []) == (["wr"], [])
    for teams in ([], ["CLE", "BAL"], ["CLE", "MIN"]):  # no team / his team / both teams
        assert linker.players_in(story, teams, []) == ([], ["justin jefferson"]), teams
    # An ESPN athlete ID still settles it.
    assert Linker(linker.players, {"7": "wr"}, linker.namesakes).players_in(story, [], ["7"])[
        0
    ] == ["wr"]

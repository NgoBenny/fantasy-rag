"""News sources -> plain story dicts (title, summary, link). Pure parsing plus a fetcher.

Only title + a short summary + the link are kept, whatever a feed offers: several feeds
include full article text, which is never stored (the `sources` registry says so).
"""

import html
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from fantasy_rag.ingest.retry import retry
from fantasy_rag.ingest.sleeper import USER_AGENT

SUMMARY_CHARS = 500
ESPN_NEWS = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?limit=50"

# nflverse team abbreviation -> (official name, team-site domain)
TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("Arizona Cardinals", "azcardinals.com"),
    "ATL": ("Atlanta Falcons", "atlantafalcons.com"),
    "BAL": ("Baltimore Ravens", "baltimoreravens.com"),
    "BUF": ("Buffalo Bills", "buffalobills.com"),
    "CAR": ("Carolina Panthers", "panthers.com"),
    "CHI": ("Chicago Bears", "chicagobears.com"),
    "CIN": ("Cincinnati Bengals", "bengals.com"),
    "CLE": ("Cleveland Browns", "clevelandbrowns.com"),
    "DAL": ("Dallas Cowboys", "dallascowboys.com"),
    "DEN": ("Denver Broncos", "denverbroncos.com"),
    "DET": ("Detroit Lions", "detroitlions.com"),
    "GB": ("Green Bay Packers", "packers.com"),
    "HOU": ("Houston Texans", "houstontexans.com"),
    "IND": ("Indianapolis Colts", "colts.com"),
    "JAX": ("Jacksonville Jaguars", "jaguars.com"),
    "KC": ("Kansas City Chiefs", "chiefs.com"),
    "LV": ("Las Vegas Raiders", "raiders.com"),
    "LAC": ("Los Angeles Chargers", "chargers.com"),
    "LA": ("Los Angeles Rams", "therams.com"),
    "MIA": ("Miami Dolphins", "miamidolphins.com"),
    "MIN": ("Minnesota Vikings", "vikings.com"),
    "NE": ("New England Patriots", "patriots.com"),
    "NO": ("New Orleans Saints", "neworleanssaints.com"),
    "NYG": ("New York Giants", "giants.com"),
    "NYJ": ("New York Jets", "newyorkjets.com"),
    "PHI": ("Philadelphia Eagles", "philadelphiaeagles.com"),
    "PIT": ("Pittsburgh Steelers", "steelers.com"),
    "SF": ("San Francisco 49ers", "49ers.com"),
    "SEA": ("Seattle Seahawks", "seahawks.com"),
    "TB": ("Tampa Bay Buccaneers", "buccaneers.com"),
    "TEN": ("Tennessee Titans", "tennesseetitans.com"),
    "WAS": ("Washington Commanders", "commanders.com"),
}


@dataclass(frozen=True)
class Feed:
    source: str  # sources.name
    url: str
    team: str | None = None  # a team site's own team


FEEDS: list[Feed] = [
    Feed("espn_news", ESPN_NEWS),
    Feed("cbs_rss", "https://www.cbssports.com/rss/headlines/nfl/"),
    Feed("yahoo_rss", "https://sports.yahoo.com/nfl/rss.xml"),
    Feed("pft_rss", "https://profootballtalk.nbcsports.com/feed/"),
    *[
        Feed("team_rss", f"https://www.{domain}/rss/news", abbr)
        for abbr, (_, domain) in TEAMS.items()
    ],
]

SOURCES = [  # registry rows: (name, license/terms, attribution); storage is summary-only
    ("espn_news", "ESPN site API (unofficial); personal, low-rate use", "News from ESPN"),
    ("cbs_rss", "CBS Sports public RSS; headline + summary + link", "News from CBS Sports"),
    ("yahoo_rss", "Yahoo Sports public RSS; headline + summary + link", "News from Yahoo Sports"),
    ("pft_rss", "NBC ProFootballTalk public RSS; headline + summary + link", "News from PFT"),
    ("team_rss", "NFL team sites' public RSS; headline + summary + link", "News from team sites"),
]  # fmt: skip


def clean(text: str | None, limit: int | None = None) -> str:
    """Strip HTML tags and entities, collapse whitespace, cut at a word boundary."""
    s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref|cmpid|partner|src)$", re.I)


def normalize_url(url: str) -> str:
    """One URL per story: https, lowercase host, no fragment or trailing slash, and no
    tracking parameters. Other query parameters stay: some sites identify stories by them."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING.match(k)])
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))  # ESPN: ISO 8601
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)  # RSS: "Tue, 20 Oct 2026 18:29:12 -0400"
        except (TypeError, ValueError):
            return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_espn(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stories = []
    for a in payload.get("articles", []):
        url = ((a.get("links") or {}).get("web") or {}).get("href")
        if not url or not a.get("headline"):
            continue
        cats = a.get("categories") or []
        stories.append(
            {
                "url": normalize_url(url),
                "title": clean(a["headline"]),
                "summary": clean(a.get("description"), SUMMARY_CHARS),
                "published_at": _when(a.get("published")),
                "espn_athlete_ids": [
                    str(c["athleteId"])
                    for c in cats
                    if c.get("type") == "athlete" and c.get("athleteId")
                ],
                "team_names": [c.get("description") for c in cats if c.get("type") == "team"],
                "tags": [],
            }
        )
    return stories


def _rss_summary(entry: Any) -> str:
    """The feed's own description only. With no description, feedparser fills `summary`
    from the full article (`content:encoded`); that is article text, so store nothing."""
    summary = entry.get("summary") or ""
    if any(summary == (c.get("value") or "") for c in entry.get("content") or []):
        return ""
    return clean(summary, SUMMARY_CHARS)


def parse_rss(raw: bytes) -> list[dict[str, Any]]:
    stories = []
    for e in feedparser.parse(raw).entries:
        if not e.get("link") or not e.get("title"):
            continue
        stories.append(
            {
                "url": normalize_url(e["link"]),
                "title": clean(e["title"]),
                "summary": _rss_summary(e),
                "published_at": _when(e.get("published") or e.get("updated")),
                "espn_athlete_ids": [],
                "team_names": [],
                "tags": [t.get("term") for t in e.get("tags", []) if t.get("term")],
            }
        )
    return stories


def fetch_all(feeds: list[Feed] = FEEDS) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Fetch every feed politely. Returns (stories by feed url, errors). A dead feed is skipped."""
    got: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as c:
        for feed in feeds:
            time.sleep(0.5)  # ~2 requests/second across all hosts
            try:
                r = retry(lambda: c.get(feed.url).raise_for_status())  # noqa: B023
                got[feed.url] = (
                    parse_espn(r.json()) if feed.source == "espn_news" else parse_rss(r.content)
                )
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{feed.url}: {exc!r}"[:300])
    return got, errors

"""Tag stories and chunks with the players and teams they mention.

Order of trust: ESPN athlete IDs (an ID join, no names), then exact full-name mentions
of current fantasy players. A full name shared by several players counts only when the
story's teams single one out; otherwise it is reported as ambiguous, never guessed.
Within a story, a later last-name mention ("Mahomes") refers back to a full-name match.
"""

import re
from dataclasses import dataclass, field

from fantasy_rag.ingest.news import TEAMS


def norm(text: str) -> str:
    """Same normalization as players.search_name, applied to free text."""
    import unicodedata

    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("-", "").replace("'", "").replace(".", ""))
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    return f" {re.sub(r'\s+', ' ', s).strip()} "


@dataclass
class Player:
    id: str
    name: str  # normalized full name, e.g. "amonra st brown"
    team: str | None


@dataclass
class Linker:
    players: list[Player]
    espn_to_player: dict[str, str] = field(default_factory=dict)
    # Active non-fantasy players sharing a fantasy player's name -> their teams
    # (e.g. "justin jefferson" -> ["CLE"] for the Browns linebacker).
    namesakes: dict[str, list[str | None]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_name: dict[str, list[Player]] = {}
        for p in self.players:
            self.by_name.setdefault(p.name, []).append(p)
        self.by_id = {p.id: p for p in self.players}
        # Longest names first so "amon ra st brown" wins over a shorter overlapping name.
        names = sorted(self.by_name, key=len, reverse=True)
        # Lookarounds don't consume the spaces, so back-to-back names ("tyler huntley
        # derrick henry") both match.
        alts = "|".join(map(re.escape, names))
        self.pattern = re.compile(f"(?<= )(?:{alts})(?= )") if names else None
        nick = {abbr: full.rsplit(" ", 1)[-1].lower() for abbr, (full, _) in TEAMS.items()}
        self.team_terms = {
            abbr: {norm(full).strip(), nick[abbr]} for abbr, (full, _) in TEAMS.items()
        }

    def teams_in(self, text: str, extra: list[str]) -> list[str]:
        t = norm(text)
        found = {a for a, terms in self.team_terms.items() if any(f" {x} " in t for x in terms)}
        full_to_abbr = {full: abbr for abbr, (full, _) in TEAMS.items()}
        found |= {full_to_abbr[n] for n in extra if n in full_to_abbr}
        found |= {n for n in extra if n in TEAMS}
        return sorted(found)

    def players_in(
        self, text: str, teams: list[str], espn_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        """(player IDs, ambiguous names) for one story."""
        ids = {self.espn_to_player[e] for e in espn_ids if e in self.espn_to_player}
        ambiguous = []
        t = norm(text)
        for name in set(self.pattern.findall(t)) if self.pattern else set():
            cands = self.by_name[name]
            others = self.namesakes.get(name, [])
            if len(cands) == 1 and not others:
                ids.add(cands[0].id)  # the only active player with this name
                continue
            # Same-named players: the story's teams must single out exactly one, and it must
            # be a fantasy player. No team, or teams fitting several of them: ambiguous.
            fits = [c for c in cands if c.team in teams]
            if len(fits) == 1 and not any(t in teams for t in others):
                ids.add(fits[0].id)
            else:
                ambiguous.append(name)
        return sorted(ids), sorted(ambiguous)

    def chunk_players(self, chunk: str, story_players: list[str]) -> list[str]:
        """Story players named in this chunk by full or last name."""
        t = norm(chunk)
        out = []
        for pid in story_players:
            p = self.by_id.get(pid)
            if p and (f" {p.name} " in t or f" {p.name.split(' ')[-1]} " in t):
                out.append(pid)
        return out

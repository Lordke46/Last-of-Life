"""Read-only adapter for licensed OpenFootball Spanish Segunda División history.

This module parses a caller-supplied/downloaded season JSON file. It does not
fetch data or write to the production database.
"""
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import unicodedata


REPOSITORY = "openfootball/football.json"
LICENSE = "CC0-1.0"
LEAGUE = "esp.2"
RAW_URL = "https://raw.githubusercontent.com/openfootball/football.json/master/{season}/es.2.json"
_SEASON_RE = re.compile(r"^(20\d{2})-(\d{2})$")


def load_json(path):
    """Load a previously obtained OpenFootball JSON file from disk."""
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _season_name(document):
    name = document.get("name")
    if not isinstance(name, str):
        raise ValueError("OpenFootball season name is missing")
    normalized = "".join(c for c in unicodedata.normalize("NFKD", name.casefold())
                        if not unicodedata.combining(c))
    if "segunda division" not in normalized:
        raise ValueError("Expected an OpenFootball Spain Segunda División file")
    found = re.search(r"(20\d{2})/(\d{2})", name)
    if not found:
        raise ValueError("Season name must contain YYYY/YY")
    return f"{found.group(1)}-{found.group(2)}"


def _fixture_key(season, match_date, round_name, team1, team2):
    teams = sorted((team1.strip().casefold(), team2.strip().casefold()))
    return season, match_date.isoformat(), round_name.strip().casefold(), *teams


def _provider_id(identity, season, match_date, round_name, home, away):
    material = "\0".join((season, match_date.isoformat(), round_name, home, away))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"openfootball:es.2:{identity}:{digest}"


def parse_es2(document, season, *, as_of=None, before=None):
    """Return completed Spanish Segunda fixtures as normalized, read-only rows.

    OpenFootball's date/time fields have no timezone. `kickoff` therefore stays
    date-only, even when a local `time` is present; provenance records both the
    precision and source time. `before` is conservative: same-date fixtures are
    excluded because their ordering against a timestamp cannot be established.
    """
    if not isinstance(document, dict) or not isinstance(document.get("matches"), list):
        raise ValueError("Expected an object with a matches array")
    season_match = _SEASON_RE.fullmatch(str(season))
    if not season_match or _season_name(document) != season:
        raise ValueError("Season argument does not match the OpenFootball file")
    as_of = as_of or date.today()
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    if not isinstance(as_of, date):
        raise TypeError("as_of must be a date or datetime")
    cutoff = before.date() if isinstance(before, datetime) else before
    if cutoff is not None and not isinstance(cutoff, date):
        raise TypeError("before must be a date or datetime")

    parsed, seen = [], {}
    for index, raw in enumerate(document["matches"]):
        if not isinstance(raw, dict):
            raise ValueError(f"match {index}: expected an object")
        try:
            match_date = date.fromisoformat(raw["date"])
            home, away = raw["team1"], raw["team2"]
            round_name = raw.get("round", "")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"match {index}: missing or invalid date/team fields") from exc
        if (not isinstance(home, str) or not home.strip() or not isinstance(away, str)
                or not away.strip() or home.strip().casefold() == away.strip().casefold()
                or not isinstance(round_name, str)):
            raise ValueError(f"match {index}: invalid team or round fields")
        home, away = home.strip(), away.strip()
        score = raw.get("score")
        if score is None or score == {} or score == []:
            continue  # fixtures/placeholders without a full-time result
        result_format = "ft"
        if isinstance(score, dict):
            if "ft" not in score:
                continue  # half-time only is not a completed result
            ft = score["ft"]
        elif isinstance(score, list) and len(score) == 2:
            # Some published football.json files encode scores as [home, away].
            # Past rows are retained, but explicitly marked because this form has
            # no separate final-status field to distinguish a reported 0-0 from
            # an unreported placeholder. Future rows remain unplayed fixtures.
            if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in score):
                raise ValueError(f"match {index}: invalid score array")
            if match_date > as_of:
                continue
            ft = score
            result_format = "score_array_status_unverified"
        else:
            raise ValueError(f"match {index}: unexpected score format")
        if (not isinstance(ft, list) or len(ft) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in ft)):
            raise ValueError(f"match {index}: invalid full-time score")
        if match_date > as_of:
            raise ValueError(f"match {index}: future fixture has a full-time score")

        # The source provides calendar dates and local times without a timezone.
        # Exclude the entire cutoff date to avoid ordering leakage on that day.
        if cutoff is not None and match_date >= cutoff:
            continue
        key = _fixture_key(season, match_date, round_name, home, away)
        score_by_team = {home.casefold(): ft[0], away.casefold(): ft[1]}
        previous = seen.get(key)
        if previous is not None:
            if previous["orientation"] != (home.casefold(), away.casefold()):
                raise ValueError(f"match {index}: duplicate fixture reverses home/away orientation")
            if previous["scores"] != score_by_team:
                raise ValueError(f"match {index}: contradictory duplicate result")
            continue  # identical duplicate entry; retain the first source orientation
        seen[key] = {"scores": score_by_team, "orientation": (home.casefold(), away.casefold())}

        identity = f"{match_date.isoformat()}:{round_name.strip()}"
        parsed.append({
            "id": _provider_id(identity, season, match_date, round_name.strip(), home, away),
            "league": LEAGUE,
            "kickoff": match_date.isoformat(),
            "home": home,
            "away": away,
            "home_goals": ft[0],
            "away_goals": ft[1],
            "completed": True,
            "status": "FT",
            "source": {
                "provider": REPOSITORY,
                "license": LICENSE,
                "dataset": RAW_URL.format(season=season),
                "season": season,
                "round": round_name.strip() or None,
                "kickoff_precision": "date",
                "result_format": result_format,
                "source_time_local_unzoned": raw.get("time") or None,
            },
        })
    return parsed

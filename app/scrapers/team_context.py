"""Sezonluk takım programlarıyla veri deliklerini onarır; resmi kulvarları ayrı tutar."""
import json
import urllib.request
from datetime import datetime, timezone, timedelta

from ..config import USER_AGENT, is_cup
from ..db import get_meta, set_meta
from . import espn


def season_year(now):
    return now.year if now.month >= 7 else now.year - 1


def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    return json.loads(espn._fetch_bytes(req, timeout=10, retries=2))


def standings_entries(payload):
    rows = []
    for group in payload.get('children', []):
        for entry in group.get('standings', {}).get('entries', []):
            team = entry.get('team') or {}
            stats = {s['name']: s.get('value') for s in entry.get('stats', [])}
            if not str(team.get('id', '')).isdigit() or stats.get('gamesPlayed') is None:
                continue
            rows.append({'id': str(team['id']), 'team': team.get('displayName'),
                         **{key: int(stats[value]) if stats.get(value) is not None else None for key, value in
                            {'p':'gamesPlayed', 'pts':'points', 'w':'wins', 'd':'ties', 'l':'losses',
                             'gf':'pointsFor', 'ga':'pointsAgainst', 'gd':'pointDifferential', 'pos':'rank'}.items()}})
    return rows


def schedule_matches(payload, team_id, since, now):
    if str(payload.get('team', {}).get('id')) != str(team_id) or not isinstance(payload.get('events'), list):
        raise ValueError('Takım programı kimliği veya events alanı doğrulanamadı')
    out = []
    for event in payload['events']:
        league = (event.get('league') or {}).get('slug')
        if not league or 'friendly' in league:
            continue
        normalized = espn.normalize_api_event(event)
        match = espn.parse_event(normalized, league) if normalized else None
        if not match or str(team_id) not in (match['home_id'], match['away_id']):
            continue
        at = datetime.fromisoformat(match['kickoff'].replace('Z', '+00:00'))
        if since <= at <= now + timedelta(days=30) and not (match['completed'] and at > now):
            out.append(match)
    return out


def league_records(conn, league, since, now):
    rows = conn.execute('SELECT home_id,away_id,home_goals,away_goals FROM matches WHERE league=? '
                        'AND completed=1 AND kickoff>=? AND kickoff<?',
                        (league, since.isoformat(), now.isoformat())).fetchall()
    result = {}
    for row in rows:
        if row['home_goals'] is None or row['away_goals'] is None:
            continue
        h, a = row['home_goals'], row['away_goals']
        for side, scored, conceded in (('home',h,a),('away',a,h)):
            tid = str(row[side+'_id'])
            values = result.setdefault(tid, {'p':0, 'pts':0, 'gf':0, 'ga':0, 'w':0,'d':0,'l':0,'gd':0})
            values['p'] += 1; values['gf'] += scored; values['ga'] += conceded
            values['pts'] += 3 if scored > conceded else 1 if scored == conceded else 0
            values['w' if scored>conceded else 'd' if scored==conceded else 'l'] += 1
            values['gd'] = values['gf']-values['ga']
    return result


def local_record(conn, league, team_id, since, now):
    return league_records(conn, league, since, now).get(str(team_id), {'p':0, 'pts':0, 'gf':0, 'ga':0, 'w':0,'d':0,'l':0,'gd':0})


def consistency(official, local):
    keys = [k for k in ('p','gf','ga','w','d','l','gd') if official.get(k) is not None]
    checks = {k:local.get(k)==official[k] for k in keys}
    internal = (official.get('gd') is None or official['gd']==official['gf']-official['ga'])
    if all(official.get(k) is not None for k in ('w','d','l')):
        internal = internal and official['w']+official['d']+official['l']==official['p']
    return {'results_match':all(checks.values()) and internal,'fields':checks,'source_arithmetic_valid':internal,
            'points_match':local.get('pts')==official.get('pts'),
            'possible_points_adjustment':official.get('pts')-local['pts'] if official.get('pts') is not None else None,
            'points_note':'Source points may include disciplinary deductions; mismatch is recorded, not silently corrected.'}


def reconcile_league(conn, league, progress=None, now=None):
    now = now or datetime.now(timezone.utc)
    if is_cup(league):
        return 0
    year = season_year(now)
    since = datetime(year, 7, 1, tzinfo=timezone.utc)
    rows = conn.execute('SELECT home_id,away_id FROM matches WHERE league=? AND kickoff>=?',
                        (league, since.isoformat())).fetchall()
    known = {str(r[k]) for r in rows for k in ('home_id', 'away_id') if str(r[k] or '').isdigit()}
    if not known:
        return 0
    key = f'team_context:{league}'
    standings_url = f'https://site.web.api.espn.com/apis/v2/sports/soccer/{league}/standings?season={year}'
    try:
        payload = fetch_json(standings_url)
        if int((payload.get('season') or {}).get('year', -1)) != year:
            raise ValueError('Puan tablosu sezonu uyuşmuyor')
        official = standings_entries(payload)
        if not official:
            raise ValueError('Puan tablosu boş')
    except Exception as exc:
        set_meta(conn, f'{key}:error', f'{type(exc).__name__}: {str(exc)[:180]}')
        return 0
    teams, count = {}, 0
    for team in official:
        tid = team['id']
        if tid not in known:
            continue
        schedule_url = f'https://site.api.espn.com/apis/site/v2/sports/soccer/all/teams/{tid}/schedule?season={year}'
        error = None
        try:
            matches = schedule_matches(fetch_json(schedule_url), tid, since, now)
            for match in matches:
                espn.upsert(conn, match)
            count += len(matches)
        except Exception as exc:
            error = f'{type(exc).__name__}: {str(exc)[:160]}'
        record = local_record(conn, league, tid, since, now)
        # Puan silme/tie-break kaynak tablosundan gelir; skor/adet tutarlılığı ayrı denetlenir.
        checks = consistency(team,record)
        complete = checks['results_match']
        teams[tid] = {'official': team, 'local': record, 'complete': complete,
                      'consistency':checks, 'schedule_checked': error is None, 'schedule_error': error, 'schedule_url': schedule_url}
        if progress:
            progress(f'{league}: {team["team"]} sezon uzlaştırma {record["p"]}/{team["p"]}')
        conn.commit()
    result = {'checked_at': now.isoformat(), 'season': year, 'standings_url': standings_url, 'teams': teams}
    set_meta(conn, key, json.dumps(result))
    set_meta(conn, f'{key}:error', '')
    return count


def load(conn, league):
    try:
        data = json.loads(get_meta(conn, f'team_context:{league}', '{}'))
        if not isinstance(data, dict) or not isinstance(data.get('teams', {}), dict):
            return {}
        for record in data.get('teams', {}).values():
            if not isinstance(record, dict) or not isinstance(record.get('official'), dict):
                return {}
            if any(not isinstance(record['official'].get(k), int) or isinstance(record['official'].get(k), bool)
                   or record['official'][k] < 0 for k in ('p','gf','ga')):
                return {}
        return data
    except (TypeError, ValueError):
        return {}

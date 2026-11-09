"""Read-only export of public sports/model data; never exports users, secrets or coupons."""
import argparse
import json
import sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db

PAYLOAD_FIELDS=('model','blend','market','pick','strongest_pick','odds','confidence',
                'confidence_label','ensemble','blend_settings','decision','calibration_status')


def export(conn):
    matches=[dict(r) for r in conn.execute(
        'SELECT id,league,kickoff,home,away,home_id,away_id,home_goals,away_goals,completed,status '
        'FROM matches ORDER BY kickoff LIMIT 100001').fetchall()]
    snapshots=[dict(r) for r in conn.execute(
        'SELECT match_id,league,kickoff,captured_at,model_version,payload '
        'FROM prediction_snapshots ORDER BY kickoff LIMIT 100001').fetchall()]
    if len(matches)>100000 or len(snapshots)>100000:
        raise ValueError('Audit export limit exceeded; narrow the period instead of silently truncating.')
    for row in snapshots:
        payload=json.loads(row['payload'])
        row['payload']={k:payload[k] for k in PAYLOAD_FIELDS if k in payload}
    return {'exported_at':datetime.now(timezone.utc).isoformat(),
            'scope':'public_fixture_results_and_model_forecasts_only',
            'matches':matches,'snapshots':snapshots}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if not db.is_postgres():raise SystemExit('Audit export requires the configured PostgreSQL source.')
    with db.session() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        result=export(conn)
    target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,default=str))
    print(f"Audit exported: {len(result['matches'])} matches, {len(result['snapshots'])} snapshots")

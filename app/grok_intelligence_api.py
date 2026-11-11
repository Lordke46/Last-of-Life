"""Opt-in, administrator-only read-only research display for a configured owner."""
import json
import os
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from . import auth, research_store as store

router = APIRouter()


@router.get('/api/grok-intelligence/{match_id}')
def intelligence(match_id: str, request: Request):
    # Mandatory backend administrator check even when the rest of the site is public.
    auth.require_admin(auth.require_user(auth.current_user(request)))
    owner = os.environ.get('RESEARCH_UI_OWNER', '')
    if os.environ.get('RESEARCH_UI_ENABLED') != '1' or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', owner):
        raise HTTPException(503, 'Araştırma görünümü kullanılamıyor')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', match_id):
        raise HTTPException(404, 'Maç bulunamadı')
    try:
        with store.ui_session() as conn:
            reports, versions, jobs = (store.table(x) for x in ('reports', 'report_revisions', 'jobs'))
            if store.postgres():
                reports, versions = 'ai_research.ui_reports', 'ai_research.ui_revisions'
                join_job, context = '', 'v.context_json'
            else:
                join_job, context = f'JOIN {jobs} j ON j.id=v.job_id ', 'j.context_json'
            rows = conn.execute(
                f'SELECT r.id AS report_id,r.mode,v.revision,v.payload_json,v.researched_at,'
                f'v.received_at,v.freshness_status,v.validation_status,{context} '
                f'FROM {reports} r JOIN {versions} v ON v.report_id=r.id '
                f'{join_job}WHERE r.owner_id=? AND r.match_id=? '
                f'ORDER BY v.received_at DESC,v.revision DESC LIMIT 30',
                (owner,match_id)).fetchall()
        output=[]
        for row in rows:
            payload=json.loads(row['payload_json']); context=json.loads(row['context_json'])
            price=context.get('prices') or {}; provenance=price.get('quote_provenance') or {}
            if not isinstance(provenance,dict): provenance={}
            limits=provenance.get('pilot_research_limitations')
            warnings=[x for x in limits if isinstance(x,str)][:10] if isinstance(limits,list) else []
            output.append({'report_id':row['report_id'],'mode':row['mode'],'revision':row['revision'],
                'researched_at':row['researched_at'],'received_at':row['received_at'],
                'freshness_status':row['freshness_status'],'validation_status':row['validation_status'],
                'report':payload,'context':{'quote_observed_at':price.get('observed_at'),
                    'quote_freshness':provenance.get('pilot_freshness_status','unknown'),
                    'snapshot_missing':context.get('prediction_snapshot') is None,'warnings':warnings}})
        return JSONResponse({'reports':output},headers={'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff'})
    except store.unavailable_errors():
        raise HTTPException(503, 'Araştırma görünümü kullanılamıyor') from None
    except (ValueError,TypeError,KeyError):
        raise HTTPException(503, 'Araştırma kaydı okunamadı') from None

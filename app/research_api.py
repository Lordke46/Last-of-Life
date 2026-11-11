"""Opt-in, machine-authenticated research API; no paid LLM clients."""
import os
import re
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import research_service as service, research_store as store
from .research_models import ResearchResult
from . import grok_skill_adapter

MAX_BODY_BYTES = 128 * 1024
NO_STORE = {'Cache-Control': 'private, no-store', 'CDN-Cache-Control': 'no-store',
            'Vercel-CDN-Cache-Control': 'no-store'}


class ResearchRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request):
            try:
                if os.environ.get('RESEARCH_API_ENABLED') != '1':
                    raise HTTPException(503, 'Araştırma API kapalı')
                request.state.research_principal = await run_in_threadpool(
                    service.authenticate, request.headers.get('authorization'),
                    request.client.host if request.client else 'unknown')
                if request.method == 'POST':
                    length = request.headers.get('content-length')
                    if length and (not length.isdecimal() or int(length) > MAX_BODY_BYTES):
                        raise HTTPException(413, 'Araştırma JSON boyut sınırı aşıldı')
                    chunks, size = [], 0
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_BODY_BYTES:
                            raise HTTPException(413, 'Araştırma JSON boyut sınırı aşıldı')
                        chunks.append(chunk)
                    request._body = b''.join(chunks)
                response = await handler(request)
                response.headers.update(NO_STORE)
                return response
            except HTTPException as exc:
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code,
                                    headers={**NO_STORE, **(exc.headers or {})})
            except (RequestValidationError, ValidationError) as exc:
                # Never echo the submitted report, token or raw validation input.
                errors = [{'loc': list(e['loc']), 'type': e['type'], 'msg': e['msg']} for e in exc.errors()]
                return JSONResponse({'detail': errors}, status_code=422, headers=NO_STORE)
            except store.unavailable_errors():
                return JSONResponse({'detail': 'Araştırma deposu hazır değil veya erişilemiyor'},
                                    status_code=503, headers=NO_STORE)
        return guarded


router = APIRouter(prefix='/api/research', tags=['research'], route_class=ResearchRoute)


@router.get('/schema')
def result_schema(request: Request):
    service.scope(request.state.research_principal, 'reports:write')
    return ResearchResult.model_json_schema()


@router.get('/jobs')
def jobs(request: Request, limit: int = Query(25, ge=1, le=100)):
    return service.list_jobs(request.state.research_principal, limit)


@router.get('/matches/{match_id}/context')
def match_context(request: Request, match_id: str, job_id: str = Query(..., max_length=100)):
    return service.match_context(request.state.research_principal, match_id, job_id)


@router.post('/jobs/{job_id}/claim')
def claim(request: Request, job_id: str):
    return service.claim(request.state.research_principal, job_id)


@router.post('/jobs/{job_id}/renew')
def renew(request: Request, job_id: str):
    lease = request.headers.get('x-research-claim', '')
    if not lease or len(lease) > 256:
        raise HTTPException(422, 'İş üstlenme başlığı gerekli')
    return service.renew_claim(request.state.research_principal, job_id, lease)


@router.post('/jobs/{job_id}/skill-results')
async def skill_results(request: Request, job_id: str):
    principal = request.state.research_principal
    service.scope(principal, 'reports:write')
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise HTTPException(415, 'application/json gerekli')
    key = request.headers.get('idempotency-key', '')
    lease = request.headers.get('x-research-claim', '')
    version = request.headers.get('x-skill-version', '')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', key) or not lease or len(lease) > 256 or not 1 <= len(version) <= 80:
        raise HTTPException(422, 'Idempotency-Key, X-Research-Claim ve X-Skill-Version gerekli')
    try:
        raw = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, {'path': '$', 'reason': 'Geçersiz JSON'}) from None
    job = await run_in_threadpool(service.assigned_job, principal, job_id)
    result = grok_skill_adapter.adapt(raw, job, version)
    return await run_in_threadpool(service.save_result, principal, job_id, result, lease, key)


@router.post('/jobs/{job_id}/results')
async def results(request: Request, job_id: str):
    principal = request.state.research_principal
    service.scope(principal, 'reports:write')
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise HTTPException(415, 'application/json gerekli')
    key = request.headers.get('idempotency-key', '')
    lease = request.headers.get('x-research-claim', '')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', key) or len(lease) > 256:
        raise HTTPException(422, 'Geçerli Idempotency-Key ve iş üstlenme başlıkları gerekli')
    result = ResearchResult.model_validate_json(await request.body())
    return await run_in_threadpool(service.save_result, principal, job_id, result, lease, key)


@router.get('/reports/{report_id}')
def report(request: Request, report_id: str, revision: int | None = Query(None, ge=1)):
    return service.read_report(request.state.research_principal, report_id, revision)

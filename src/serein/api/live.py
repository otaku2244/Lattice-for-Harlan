"""Authenticated client compatibility routes for the writable private service."""

import json

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..compat.events import Events
from ..compat.germany.fact_events import FactEventSettlementBlockedError
from ..core.domains import canonical_domain
from ..core.store import Store


def domain_policies(database):
    """Published domain rules, as (flat, per_kind).

    Recall resolves a domain against ``domain_rules[kind]`` and only then falls
    back to the flat ``domains`` map, so a label built from the per-kind map alone
    would call a restricted domain "normal". Both maps must reach the client.
    """
    with Store(database, read_only=True) as store:
        row = store.conn.execute(
            "SELECT value_json FROM background_state WHERE name='deployment_settings'"
        ).fetchone()
    if not row:
        return {}, {}
    tagging = (json.loads(row[0]) or {}).get('tagging') or {}
    flat = {item['key']: item.get('policy', 'normal')
            for item in tagging.get('domains', []) if isinstance(item, dict) and item.get('key')}
    return flat, (tagging.get('policies') or {})


def effective_domain_policy(flat, per_kind, kind, domain):
    """Mirror the resolution order in recall.scene.domain_rejection."""
    rules = per_kind.get(kind) or {}
    if domain in rules:
        return rules[domain]
    return flat.get(domain, 'normal')


def event_surface_states(database, result):
    """Attach surfacing state and the effective domain rule to every Event.

    The stored manual_surface column and the published domain rule are two
    separate gates: recall already rejects a domain before manual_surface is
    consulted, so the client label must show both or it reports a stale
    "auto surfaces" verdict for every Event in a restricted domain.
    """
    flat, per_kind = domain_policies(database)
    with Store(database, read_only=True) as store:
        for item in result.get('items', []):
            if item.get('item_type') != 'event':
                continue
            item['surface_state'] = store.surface_state(item['item_id'])
            document = store.read(item['item_id'])
            domain = canonical_domain(document['metadata']) if document else None
            item['canonical_domain'] = domain
            item['domain_policy'] = effective_domain_policy(
                flat, per_kind, item['item_type'], domain)
    return result


def routes(settings, services, auth):
    router = APIRouter(dependencies=auth)
    events = Events(settings.database)

    @router.get('/api/fact-events')
    def list_events(type: str='', status: str='active', date: str='', query: str='',
                    limit: int=Query(100,ge=1,le=500), offset: int=Query(0,ge=0), include_sources: bool=False):
        return event_surface_states(settings.database,events.list(item_type=type,status=status,date=date,query=query,limit=limit,offset=offset,include_sources=include_sources))

    @router.post('/api/fact-events/read-many')
    def read_many(body: dict):
        return event_surface_states(settings.database,events.read_many(body.get('item_ids'), include_sources=bool(body.get('include_sources',True)),
                                resolve_active_successors=bool(body.get('resolve_active_successors',False))))

    @router.post('/api/fact-events/find-by-source-keys')
    def find_sources(body: dict):
        return events.find_active_events_by_source_keys(body.get('source_keys'))

    @router.get('/api/fact-events/active-leaf')
    def active_leaf(item_id: str):
        return events.replacement_family(item_id)

    def written(result):
        # Indexing errors do not turn a committed settlement into an HTTP failure.
        # A persistent worker retries the durable outbox, including vector fills.
        return {**result,'index_status':'queued'}

    @router.post('/api/fact-events/batch')
    def batch(body: dict):
        return written(events.write_many(body.get('items')))

    @router.post('/api/fact-events/settlement')
    def settle(body: dict):
        try:
            return written(events.settle(body.get('operation_id'),body.get('items')))
        except FactEventSettlementBlockedError as error:
            return JSONResponse(status_code=409,content={'ok':False,'status':'blocked',
                'operation_id':body.get('operation_id'),'reason':str(error),'writes_performed':[]})

    @router.post('/api/fact-events/replacements')
    def replacements(body: dict):
        return written(events.replace_many(body.get('items')))

    @router.post('/api/fact-events/revise')
    def revise(body: dict):
        return written(events.revise(body.get('item_id',''),**{k:v for k,v in body.items() if k in ('title','body','importance','recallable')}))

    @router.post('/api/fact-events/rewrite')
    async def rewrite(body: dict):
        """Local fork: 让 event_writer 重新拟稿并落库，不经过人工预览。

        重写口径是 create（不给旧稿），机械校验不过就不落库——宁可报错也不写入更差的稿。
        """
        from ..extensions.event_rewrite import rewrite_event_draft
        item_id = str(body.get('item_id') or '').strip()
        if not item_id:
            return JSONResponse(status_code=400,content={'ok':False,'status':'invalid',
                'message':'缺少 item_id。'})
        try:
            draft = await rewrite_event_draft(settings.database,item_id)
        except ValueError as error:
            return JSONResponse(status_code=404,content={'ok':False,'status':'not_found',
                'message':str(error)})
        except Exception as error:
            return JSONResponse(status_code=502,content={'ok':False,'status':'upstream_error',
                'message':'重写调用模型失败：%s' % error})
        if not draft.get('ok'):
            return JSONResponse(status_code=422,content={'ok':False,'status':'rejected',
                'message':'重写未通过机械校验，未落库。','violations':draft.get('violations'),
                'attempts':draft.get('attempts'),'history':draft.get('history')})
        return written(events.revise(item_id,title=draft['title'],body=draft['body']))

    @router.post('/api/fact-events/status')
    def status(body: dict):
        return written(events.set_status(body.get('item_id',''),body.get('status','')))

    @router.post('/api/fact-events/delete')
    def delete(body: dict):
        return written(events.delete(body.get('item_id','')))

    @router.post('/api/fact-events/recallable-review')
    def review(body: dict):
        return written(events.review_event_recallable(body.get('reviews'),apply=bool(body.get('apply',False))))

    @router.post('/api/fact-events/injected')
    def injected(body: dict):
        return events.mark_injected(body.get('item_ids'))

    @router.post('/api/fact-events/relation-candidates')
    def relation_candidates(body: dict):
        return events.relation_candidates(body.get('new_item_ids'),limit_per_fact=body.get('limit_per_fact',30))

    @router.post('/api/fact-events/relation-proposals')
    def propose(body: dict):
        return events.propose_relations(body.get('proposals'))

    @router.get('/api/fact-events/relation-proposals')
    def proposals(status: str='pending', limit: int=100):
        return events.list_relation_proposals(status=status,limit=limit)

    return router

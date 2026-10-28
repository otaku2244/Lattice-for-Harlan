"""Explicit window writing and latest-window continuity; no model is called here."""
import json

from ..core.store import Store, Conflict, encode, digest, now

WRITING_RULE = '''Write the window shadow before a handoff (换窗/归档前必调)。每次调用会同步 identity：
user_view → user_description（用户介绍，≤2000字），self_view → ai_description（AI介绍，≤2000字）；
recent_events 只留在窗影正文。空字段保留原值、不覆盖。
本工具不生成 Scene，不产出独立画像。
【写前必做】先 window_shadow_read(index=1) 取上一版，逐句自问：这句今天还成立吗？
成立则留，不成立才改。禁止凭记忆整篇重写。
【user_view · 骨相层，≤800字】
只写跨窗成立的东西：稳定性情、核心心智、精神审美、已被反复证实的相处契约。
禁三类句式：
· 时间状语——最近／这几天／这一阵
· 趋势判断——变得更／开始有点／越来越
· 具体场景——那晚／今晚／那次
自检：摘掉时间状语后若说不通，它不属于这里。
篇幅即约束：800字装不下临时状态。
【self_view · 校准层】
只写对自己的认知微调：调准了什么分寸、戒掉了什么口癖、确立了哪条边界。
复述本窗做过的事属越界——那是 recent_events 的活。
【recent_events · 倒渣存骨层】纯客观。禁抒情、禁感慨、禁"我们度过了一个……"类总结句。
四格必须齐，缺则写"无"：
1) 时间与坐标：
2) 主线与决议：谁拍的板、否了什么、为什么否
3) 硬资产：文件名／模型名／配置项／URL／版本号／设备路径
4) 可回捞 cues：2~4 个检索词
尾部不得省略：换窗前的最后一段对话必须完整入库，禁止用"准备换窗"一句带过。
记不清的写 [待核]，不许编。
【scene_ids】可选。本窗升格过的 Scene 挂进来，让下一窗能顺着指针回捞原件。
重试：复用同一 window_id 与 expected_revision。'''


def latest_shadow(store):
    return store.conn.execute("SELECT * FROM historical_works WHERE kind='shadow' "
        "AND id NOT IN (SELECT document_id FROM deletions) "
        "ORDER BY json_extract(metadata_json,'$.created_at') DESC,id DESC LIMIT 1").fetchone()


def sync_introductions(store, sections):
    from ..deployment import read_from_store
    state = read_from_store(store)
    for field, section in [('user_description','user_view'),('ai_description','self_view')]:
        if sections.get(section):
            state['identity'][field] = sections[section]
    store.conn.execute("INSERT INTO background_state(name,value_json) VALUES ('deployment_settings',?) "
        "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json", (encode(state),))


class WindowShadows:
    def __init__(self, database):
        self.database = database

    def write(self, window_id: str, title: str, content: str = '', scene_ids: list[str] | None = None,
              expected_revision: int = 0, user_view: str | None = None,
              self_view: str | None = None, recent_events: str | None = None):
        """Write before handoff: user_view=我眼中的你, self_view=我眼中的自己, recent_events=这一窗发生的事.
        The latest saved window updates editable introductions. Empty views leave introductions unchanged.
        Reuse window_id/revision on retries. This never generates Scenes or a separate portrait.
        """
        if not window_id.strip() or len(window_id)>200 or not title.strip():
            raise ValueError('Window ID and title are required')
        sections = {'user_view':user_view, 'self_view':self_view, 'recent_events':recent_events}
        structured = any(value is not None for value in sections.values())
        sections = {key:(value or '').strip() for key,value in sections.items()}
        if any(len(sections[key])>2000 for key in ('user_view','self_view')) or len(sections['recent_events'])>20000:
            raise ValueError('Views allow 2000 characters each; recent events allow 20000')
        if structured:
            content = '\n\n'.join('## '+label+'\n\n'+sections[key] for key,label in
                [('user_view','我眼中的你'),('self_view','我眼中的自己'),('recent_events','这一窗发生的事')]
                if sections[key])
        if not content.strip():
            raise ValueError('Write at least one window section')
        key = 'shadow:'+window_id
        scene_ids = list(dict.fromkeys(scene_ids or []))
        with Store(self.database) as store, store.transaction(immediate=True):
            for scene in scene_ids:
                if not store.conn.execute("SELECT 1 FROM documents WHERE id=? AND kind='scene' AND lifecycle='active'",(scene,)).fetchone():
                    raise ValueError('Window shadow references an unavailable Scene')
            old = store.conn.execute("SELECT * FROM historical_works WHERE id=?",(key,)).fetchone()
            if store.conn.execute('SELECT 1 FROM deletions WHERE document_id=?',(key,)).fetchone():
                raise Conflict('A deleted window shadow cannot be overwritten')
            meta = json.loads(old['metadata_json']) if old else {}
            if old and old['title']==title and old['body_md']==content and meta.get('moment_bucket_ids_json')==encode(scene_ids) and meta.get('sections_json')==encode(sections if structured else {}):
                return {'id':key,'window_id':window_id,'revision':old['revision'],'status':'unchanged','ordinary_recall':False}
            if (old['revision'] if old else 0)!=expected_revision:
                raise Conflict('Read the current window shadow revision before editing')
            revision=expected_revision+1
            meta.update(window_id=window_id,revision_root_id=window_id,revision=revision,title=title,
                created_at=meta.get('created_at') or now(),updated_at=now(),
                sections_json=encode(sections if structured else {}),moment_bucket_ids_json=encode(scene_ids))
            store.conn.execute("INSERT INTO historical_works (id,kind,revision,title,body_md,body_sha256,metadata_json,origin,source_path) "
                "VALUES (?,'shadow',?,?,?,?,?,'authored','') ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,"
                "title=excluded.title,body_md=excluded.body_md,body_sha256=excluded.body_sha256,metadata_json=excluded.metadata_json",
                (key,revision,title,content,digest(content),encode(meta)))
            if structured and latest_shadow(store)['id']==key:
                sync_introductions(store,sections)
        return {'id':key,'window_id':window_id,'revision':revision,'status':'saved','ordinary_recall':False}

    def read(self, window_id: str = ''):
        """Read the shadow's full content once, with section headings preserved. Omit window_id for the latest; explicit IDs are for history/editing."""
        with Store(self.database,read_only=True) as store:
            row = store.conn.execute("SELECT * FROM historical_works WHERE kind='shadow' AND id IN (?,?) "
                "AND id NOT IN (SELECT document_id FROM deletions)",(window_id,'shadow:'+window_id)).fetchone() if window_id else latest_shadow(store)
            if row is None:return {'status':'not_found'}
            meta=json.loads(row['metadata_json'])
            return {'status':'ok','id':row['id'],'window_id':meta.get('window_id'), 'title':row['title'],
                'content':row['body_md'],'revision':row['revision'],
                'scene_ids':json.loads(meta.get('moment_bucket_ids_json') or '[]'),'ordinary_recall':False,
                'writing_rule':WRITING_RULE}

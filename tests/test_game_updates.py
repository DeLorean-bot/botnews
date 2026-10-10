import asyncio
import json
from types import SimpleNamespace
import pytest
from botnews.sources import steam_posts, validate_source
from botnews.analysis import Analyzer, update_download_size
from botnews.storage import Store
from botnews.models import now
from botnews.service import Service
from botnews.telegram_sender import message_text

def patch_data():
    return dict(relevant=True,substantial=True,category='cs2_update',title='Обновление CS2: исправление звука',
                summary='Valve исправила звук перезарядки оружия.',importance='Исправление касается звука.',
                confirmed=[{'fact':'Исправлен звук перезарядки оружия.','quote':'Fixed reload sound'}],
                unknown='Другие подробности не указаны.',novelty=.1,impact=.1,evidence=1,confidence='high',
                event_key='cs2-update',event_date=None,sensitive_claim=False)

def test_steam_official_only():
    base = dict(appid=730,feedname='steam_community_announcements',gid='123',title='Counter-Strike 2 Update',
                contents='[p]Fixed reload sound[/p]',date=1791499009,url='https://store.steampowered.com/news/app/730/view/123')
    payload={'appnews':{'appid':730,'newsitems':[base,dict(base,gid='124',title='Discount sale'),dict(base,gid='125',feedname='pcgamer') ]}}
    posts=steam_posts(payload,{'id':1})
    assert len(posts)==1
    assert '[p]' not in posts[0].text
    assert posts[0].published
    with pytest.raises(ValueError):
        validate_source('steam_updates','https://example.org/news/updates')

def test_size_requires_explicit_download_label():
    assert update_download_size('Patch download size: 1.25 GB')=='1.25 GB'
    assert update_download_size('Размер обновления: 350 МБ')=='350 МБ'
    assert update_download_size('Requires 85 GB of disk space; memory: 800 MB') is None
    assert update_download_size('Reload takes 2.5 seconds') is None

def test_minor_official_patch_bypasses_hvh_filter(tmp_path):
    store=Store(str(tmp_path/'db.sqlite3'))
    store.add_source('Valve','steam_updates','https://www.counter-strike.net/news/updates',1)
    from botnews.models import Post
    pid=store.ingest(Post(1,'https://store.steampowered.com/news/app/730/view/123','Counter-Strike 2 Update','Fixed reload sound',now()))
    analyzer=Analyzer(store,None,SimpleNamespace(api_key='test',max_age=72))
    async def semantic(post):return patch_data(),True
    analyzer.semantic=semantic
    row=store.rows('SELECT * FROM posts WHERE id=?',(pid,))[0]
    row.update(source_kind='steam_updates',reliability=1)
    asyncio.run(analyzer.analyze(row))
    saved=store.rows('SELECT * FROM posts')[0]
    assert saved['status']=='ready' and saved['score']<75
    data=json.loads(saved['analysis'])
    assert data['official_update'] and data['download_size'] is None
    saved['source_name']='Valve'
    text=message_text(saved,data)
    assert 'Valve не указала' in text
    assert 'Что изменилось' in text
    config=SimpleNamespace(mode='critical',threshold=90,channel=123,research_channel=0,max_age=72)
    service=Service(store,None,config)
    calls=[]
    async def send(*args):calls.append(args);return SimpleNamespace(id=5)
    service.sender=send
    asyncio.run(service.publish())
    assert len(calls)==1
    store.db.close()

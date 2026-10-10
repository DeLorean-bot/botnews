import asyncio
import json
from types import SimpleNamespace
import pytest
from botnews.models import Post, canonical, now
from botnews.storage import Store
from botnews.sources import telegram_posts, forum_links, forum_post, rss_posts, validate_source
from botnews.analysis import prefilter, similarity, validate_analysis, Analyzer
from botnews.service import Service
from botnews.discord_bot import NewsBot, embed
from botnews.config import Config
from botnews.telegram_sender import TelegramSender, message_text

@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / 'test.sqlite3'))
    s.add_source('source', 'telegram', 'https://t.me/neverlosecc', .8)
    yield s
    s.db.close()

def data(**overrides):
    d = dict(relevant=True, substantial=True, category='project_update', title='Изменён сетевой компонент',
        summary='Разработчики переработали обработку сетевых данных. Это может повлиять на совместимость.',
        importance='Изменение может потребовать адаптации инструментов.', confirmed=[{'fact':'Опубликовано описание изменения протокола.', 'quote':'protocol architecture'}],
        unknown='Практический эффект не проверен.', novelty=1, impact=1, evidence=1, confidence='high',
        event_key='cs2-protocol-v2', event_date=None, sensitive_claim=False)
    d.update(overrides)
    return d

def test_prefilter_noise_and_substantial():
    assert not prefilter('CS2 cfg promo code discount buy now ' * 10)[0]
    assert not prefilter('CS2 updated today with fixes ' * 10)[0]
    assert prefilter('CS2 protocol architecture compatibility research regression ' * 4)[0]

def test_canonical_and_ingest(store):
    assert canonical('https://t.me/neverlosecc/1?utm_source=x#y') == 'https://t.me/neverlosecc/1'
    p = Post(1, 'https://t.me/neverlosecc/1', 'test', 'CS2 protocol architecture')
    assert store.ingest(p)
    assert store.ingest(p) is None
    store.backup(str(__import__('pathlib').Path(store.db.execute('PRAGMA database_list').fetchone()[2]).parent / 'backup.db'))

def test_parsers():
    source = {'id':1}
    html = '<div class="tgme_widget_message" data-post="neverlosecc/5"><div class="tgme_widget_message_text">CS2 protocol research<a href="https://example.com">link</a></div><time datetime="2026-10-10T01:00:00+00:00"></time></div>'
    p = telegram_posts(html, source)[0]
    assert p.url == 'https://t.me/neverlosecc/5'
    assert p.published and p.links == ['https://example.com']
    listing = '<div class="structItem-title"><a href="/threads/test.10/">test</a></div>'
    assert forum_links(listing, 'https://yougame.biz/forums/1075/') == ['https://yougame.biz/threads/test.10/']
    thread = '<h1>Research</h1><article class="message"><time datetime="2026-10-10T01:00:00Z"></time><div class="bbWrapper">CS2 protocol<blockquote>copied</blockquote></div></article>'
    assert forum_post(thread, source, 'https://yougame.biz/threads/test.10/').text == 'CS2 protocol'
    xml = '<rss><channel><item><title>CS2</title><link>https://example.com/1</link><description>protocol</description><pubDate>Sat, 10 Oct 2026 01:00:00 GMT</pubDate></item></channel></rss>'
    assert rss_posts(xml, source)[0].published == '2026-10-10T01:00:00+00:00'

@pytest.mark.parametrize('url', ['http://t.me/test', 'https://t.me/+invite', 'https://user:pass@t.me/test'])
def test_invalid_urls(url):
    with pytest.raises(ValueError):
        validate_source('telegram', url)

def test_grounding_and_paraphrase():
    assert validate_analysis(data(), 'CS2 protocol architecture research')['relevant']
    with pytest.raises(ValueError):
        validate_analysis(data(), 'Unrelated source')
    with pytest.raises(ValueError):
        validate_analysis(data(summary='CS2 protocol architecture research'), 'CS2 protocol architecture research')
    assert 'проверка' in validate_analysis(data(unknown=''), 'CS2 protocol architecture research')['unknown']
    with pytest.raises(ValueError, match='Russian'):
        validate_analysis(data(summary='The project announced a protocol change.'), 'CS2 protocol architecture research')
    with pytest.raises(ValueError, match='attribution'):
        validate_analysis(data(), 'Neverlose CS2 protocol architecture research')

def test_score_caps(store):
    config = SimpleNamespace(max_age=72)
    analyzer = Analyzer(store, None, config)
    p = {'body':'CS2 VAC bypass research', 'reliability':1, 'published':now()}
    assert analyzer.score(p, data(sensitive_claim=True), []) <= 74
    assert analyzer.score(p, data(sensitive_claim=True), ['https://example.org/evidence']) >= 90
    assert analyzer.score(dict(p, published=None), data(), ['https://example.org']) <= 74
    assert analyzer.score(p, data(confidence='low'), []) <= 54

def test_feedback_bounded(store):
    analyzer = Analyzer(store, None, SimpleNamespace(max_age=72))
    p = {'body':'CS2 protocol architecture', 'reliability':1, 'published':now()}
    initial = analyzer.score(p, data(), [])
    for n in range(10):
        store.execute('INSERT INTO feedback VALUES(?,?,?,?,?,?)', (n, 1, 'reject','low_significance','project_update', now()))
    assert initial - analyzer.score(p, data(), []) == 10
    assert store.rows('SELECT enabled FROM sources')[0]['enabled'] == 1

def test_duplicate_does_not_call_llm(store):
    body = 'CS2 protocol architecture research compatibility ' * 4
    old = store.ingest(Post(1,'https://t.me/neverlosecc/1','first',body))
    store.execute("UPDATE posts SET status='ready' WHERE id=?", (old,))
    new = store.ingest(Post(1,'https://t.me/neverlosecc/2','second',body))
    analyzer = Analyzer(store, None, SimpleNamespace(api_key='unused'))
    asyncio.run(analyzer.analyze(store.rows('SELECT * FROM posts WHERE id=?',(new,))[0]))
    assert store.rows('SELECT status FROM posts WHERE id=?',(new,))[0]['status'] == 'duplicate'

def test_no_model_no_publication(store):
    post_id = store.ingest(Post(1,'https://t.me/neverlosecc/1','first','CS2 protocol architecture research compatibility ' * 4))
    asyncio.run(Analyzer(store,None,SimpleNamespace(api_key='')).analyze(store.rows('SELECT * FROM posts')[0]))
    assert store.rows('SELECT status FROM posts WHERE id=?',(post_id,))[0]['status'] == 'needs_model'

def test_commands_and_embed(store, monkeypatch):
    for name in ('DISCORD_TOKEN','DISCORD_CHANNEL_ID','DISCORD_ADMIN_IDS','OPENAI_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    config = Config.load()
    bot = NewsBot(config, store, SimpleNamespace())
    assert len(bot.tree.get_commands()) == 10
    p = {'id':1,'score':80,'url':'https://t.me/neverlosecc/1','published':None,'discovered':now(),'source_name':'Neverlose'}
    result = embed(p, data()).to_dict()
    assert 'protocol architecture' not in json.dumps(result,ensure_ascii=False)  # internal quote is never sent
    assert len(embed(p, data())) < 6000

def test_delivery_is_not_repeated(store):
    config = SimpleNamespace(mode='high_signal',threshold=75,channel=123,research_channel=0,max_age=72)
    service = Service(store,None,config)
    post_id = store.ingest(Post(1,'https://t.me/neverlosecc/1','first','body',now()))
    store.execute("UPDATE posts SET status='ready',score=80,analysis=? WHERE id=?",(json.dumps(data()),post_id))
    calls = []
    async def send(*args):
        calls.append(args)
        return SimpleNamespace(id=456)
    service.sender = send
    asyncio.run(service.publish())
    asyncio.run(service.publish())
    assert len(calls) == 1

def test_uncertain_delivery_requires_review(store):
    config = SimpleNamespace(mode='high_signal',threshold=75,channel=123,research_channel=0,max_age=72)
    service = Service(store,None,config)
    post_id = store.ingest(Post(1,'https://t.me/neverlosecc/1','first','body',now()))
    store.execute("UPDATE posts SET status='ready',score=80,analysis=? WHERE id=?",(json.dumps(data()),post_id))
    calls = []
    async def send(*args):
        calls.append(args)
        raise TimeoutError('ambiguous')
    service.sender = send
    asyncio.run(service.publish())
    asyncio.run(service.publish())
    assert len(calls) == 1
    assert store.rows('SELECT state FROM deliveries')[0]['state'] == 'uncertain'

def test_telegram_topic_and_escaping(monkeypatch):
    monkeypatch.setenv('TELEGRAM_THREAD_ID', '70')
    p = {'id':1,'score':80,'url':'https://t.me/neverlosecc/1','source_name':'Test <unsafe>'}
    text = message_text(p, data(title='News <script>'))
    assert '&lt;script&gt;' in text
    assert 'protocol architecture' not in text
    captured = []
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return {'ok':True,'result':{'message_id':8}}
    class Session:
        def post(self, url, json):
            captured.append(json)
            return Response()
    result = asyncio.run(TelegramSender(Session(),'not-a-real-token')(-1004340834224,p,data()))
    assert result.id == 8
    assert captured[0]['message_thread_id'] == 70
    assert captured[0]['chat_id'] == -1004340834224

def test_telegram_error_hides_token(monkeypatch):
    monkeypatch.delenv('TELEGRAM_THREAD_ID', raising=False)
    class Session:
        def post(self, *args, **kwargs):
            raise ValueError('https://api.telegram.org/botSECRET/sendMessage')
    p = {'id':1,'score':80,'url':'https://t.me/neverlosecc/1','source_name':'Test'}
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(TelegramSender(Session(),'SECRET')(123,p,data()))
    assert 'SECRET' not in str(exc.value)

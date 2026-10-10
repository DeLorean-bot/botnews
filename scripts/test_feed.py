"""Explicit one-off replay of available real posts, isolated from production state.

Run with env credentials. All output messages carry TEST and original publication date.
The normal relevance/score rules remain active; the historical age gate is deliberately
disabled for this manual replay. Delivery intents persist to avoid blind resend on retry.
"""
import argparse
import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import aiohttp
from botnews.analysis import Analyzer, prefilter
from botnews.config import Config
from botnews.models import now
from botnews.sources import Fetcher, collect
from botnews.storage import Store
from botnews.telegram_sender import message_text

log = logging.getLogger(__name__)

def test_message(post, analysis):
    raw_date = post.get('published')
    date = (datetime.fromisoformat(raw_date.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M') + ' МСК') if raw_date else 'неизвестна'
    return '<b>🧪 ТЕСТ — архивная публикация</b>\nИсходная дата: ' + html.escape(date) + '\n\n' + message_text(post,analysis)

async def send_text(session, text):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    payload = {'chat_id':int(os.environ['TELEGRAM_CHAT_ID']), 'message_thread_id':int(os.environ.get('TELEGRAM_THREAD_ID') or 0),
               'text':text,'parse_mode':'HTML','link_preview_options':{'is_disabled':True},'disable_notification':True}
    if not payload['message_thread_id']:
        payload.pop('message_thread_id')
    # Never expose the token-bearing URL through aiohttp exception strings.
    for attempt in range(3):
        try:
            async with session.post('https://api.telegram.org/bot'+token+'/sendMessage',json=payload) as response:
                result = await response.json()
                if response.status == 429:
                    await asyncio.sleep(min(60, result.get('parameters',{}).get('retry_after',5)))
                    continue
                if response.status != 200 or not result.get('ok'):
                    raise RuntimeError('Telegram rejected test delivery: HTTP '+str(response.status))
                return result['result']['message_id']
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError('Test delivery result uncertain; reconcile before retry') from None
    raise RuntimeError('Telegram test delivery rate limit')

async def run(args):
    config=Config.load()
    if not config.api_key:
        raise ValueError('Model API key required for actual paraphrases')
    if args.send and (not os.getenv('TELEGRAM_BOT_TOKEN') or not os.getenv('TELEGRAM_CHAT_ID')):
        raise ValueError('Telegram credentials required')
    store=Store(args.database)
    report={'started':now(),'sources':[],'posts':[]}
    try:
        store.seed(config.sources)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            fetcher=Fetcher(session)
            for source in store.rows('SELECT * FROM sources WHERE enabled=1'):
                try:
                    posts=await collect(source,fetcher)
                    for post in posts:
                        store.ingest(post)
                    report['sources'].append({'name':source['name'],'count':len(posts)})
                    log.info('Collected source %s: %s posts',source['id'],len(posts))
                except Exception as exc:
                    report['sources'].append({'name':source['name'],'error':str(exc)})
                    log.warning('Source %s unavailable: %s',source['id'],exc)
            analyzer=Analyzer(store,session,config)
            posts=store.rows('''SELECT p.*,s.name AS source_name,s.kind AS source_kind,s.reliability FROM posts p JOIN sources s ON s.id=p.source_id ORDER BY p.id''')
            for index,post in enumerate(posts,1):
                eligible = post['source_kind']=='steam_updates' or prefilter(post['body'])[0]
                if eligible:
                    for attempt in range(3):
                        try:
                            used=await analyzer.analyze(post)
                            if used:
                                await asyncio.sleep(args.model_spacing)
                            break
                        except Exception as exc:
                            if attempt==2:
                                store.execute("UPDATE posts SET status='failed',error=? WHERE id=?",(str(exc)[:500],post['id']))
                            else:
                                log.warning('Analysis %s retry: %s',post['id'],exc)
                                await asyncio.sleep(20)
                else:
                    await analyzer.analyze(post)
                current=store.rows('SELECT * FROM posts WHERE id=?',(post['id'],))[0]
                current.update(source_name=post['source_name'])
                analysis=json.loads(current['analysis']) if current['analysis'] else None
                passed=bool(analysis and (analysis.get('official_update') or current['score']>=config.threshold) and current['status'] not in {'duplicate','rejected','failed'})
                item={'id':post['id'],'source':post['source_name'],'url':post['url'],'published':post['published'],
                      'status':current['status'],'score':current['score'],'passed':passed,'reason':current['error']}
                if analysis:
                    item['analysis']=analysis
                if passed:
                    text=test_message(current,analysis)
                    item['preview']=text
                    channel=int(os.getenv('TELEGRAM_CHAT_ID') or 0)
                    if args.send and not store.rows('SELECT * FROM deliveries WHERE post_id=? AND channel_id=?',(post['id'],channel)):
                        store.execute("INSERT INTO deliveries VALUES(?,?,NULL,'sending',?)",(post['id'],channel,now()))
                        try:
                            mid=await send_text(session,text)
                            store.execute("UPDATE deliveries SET state='sent',message_id=? WHERE post_id=? AND channel_id=?",(mid,post['id'],channel))
                            item['message_id']=mid
                            await asyncio.sleep(3)
                        except Exception as exc:
                            store.execute("UPDATE deliveries SET state='uncertain' WHERE post_id=? AND channel_id=?",(post['id'],channel))
                            item['delivery_error']=str(exc)
                    elif args.send:
                        item['delivery_skipped']='Already recorded; no blind repeat'
                report['posts'].append(item)
                Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                log.info('Test progress %s/%s: %s score=%s',index,len(posts),current['status'],current['score'])
            sent=sum('message_id' in item for item in report['posts'])
            counts={status:sum(p['status']==status for p in report['posts']) for status in {p['status'] for p in report['posts']}}
            report.update(finished=now(),sent=sent,counts=counts)
            Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            if args.send:
                text=f'<b>🧪 Тестовый прогон завершён</b>\nПроверено публикаций: {len(posts)}\nОтправлено тестовых новостей: {sent}\nПропущено/отсеяно: {len(posts)-sum(p["passed"] for p in report["posts"])}\nНедоступных источников: {sum("error" in s for s in report["sources"])}\n\nОбычный мониторинг новых событий продолжает работать.'
                await send_text(session,text)
            print(json.dumps({'collected':len(posts),'sent':sent,'counts':counts}))
    finally:
        store.db.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--send',action='store_true',help='Actually send TEST posts to configured Telegram topic')
    parser.add_argument('--database',default='data/test-feed.sqlite3')
    parser.add_argument('--report',default='data/test-feed-report.json')
    parser.add_argument('--model-spacing',type=float,default=12)
    args=parser.parse_args()
    if Path(args.database).resolve()==Path(Config.load().database).resolve():
        raise ValueError('Test replay requires a separate database')
    Path(args.report).parent.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(run(args))

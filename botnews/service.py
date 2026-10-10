import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from .analysis import Analyzer
from .sources import collect, Fetcher
from .models import now
from .config import MODES

log = logging.getLogger(__name__)

class Service:
    def __init__(self, store, session, config):
        self.store, self.config = store, config
        self.fetcher = Fetcher(session)
        self.analyzer = Analyzer(store, session, config)
        self.cycle_lock = asyncio.Lock()
        self.sender = None

    async def poll_source(self, source):
        try:
            posts = await collect(source, self.fetcher)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=self.config.max_age)
            for p in posts:
                old = p.published and datetime.fromisoformat(p.published.replace('Z', '+00:00')) < cutoff
                self.store.ingest(p, baseline=bool(old or (not source['initialized'] and not self.config.backfill)))
                for link in p.links:
                    self.store.execute('INSERT OR IGNORE INTO suggestions VALUES(?,?,?)', (link, source['id'], now()))
            self.store.execute('UPDATE sources SET initialized=1,last_ok=?,error=NULL,failures=0,next_poll=? WHERE id=?',
                               (now(), time.time() + self.config.poll, source['id']))
        except Exception as exc:
            log.warning('Source %s: %s', source['name'], exc)
            failures = source['failures'] + 1
            self.store.execute('UPDATE sources SET error=?,failures=?,next_poll=? WHERE id=?',
                (str(exc)[:500], failures, time.time() + min(3600, self.config.poll * 2 ** min(failures, 5)), source['id']))

    async def process(self):
        if self.config.api_key:
            self.store.execute("UPDATE posts SET status='pending' WHERE status='needs_model'")
        budget = self.config.llm_budget
        posts = self.store.rows('''SELECT p.*,s.reliability,s.name AS source_name FROM posts p JOIN sources s ON p.source_id=s.id
                  WHERE p.status='pending' AND p.next_retry<=? ORDER BY p.id LIMIT 100''', (time.time(),))
        for p in posts:
            if budget <= 0:
                break
            try:
                # Reserve budget before request; failures also consume the cycle limit.
                budget -= 1
                await self.analyzer.analyze(p)
            except Exception as exc:
                attempts = p['attempts'] + 1
                status = 'failed' if attempts >= 5 else 'pending'
                self.store.execute('UPDATE posts SET attempts=?,next_retry=?,status=?,error=? WHERE id=?',
                    (attempts, time.time() + min(3600, 60 * 2 ** attempts), status, str(exc)[:500], p['id']))
                log.warning('Analysis %s failed: %s', p['id'], exc)
        # Reevaluate scores when later independent evidence arrives.
        for p in self.store.rows('''SELECT p.*,s.reliability FROM posts p JOIN sources s ON p.source_id=s.id
                                    WHERE p.status IN ('research','ready') AND p.analysis IS NOT NULL'''):
            data = json.loads(p['analysis'])
            data['confirmations'] = self.analyzer.evidence(p, data)
            score = self.analyzer.score(p, data, data['confirmations'])
            status = 'ready' if score >= 75 else 'research' if score >= 55 else 'rejected'
            self.store.execute('UPDATE posts SET score=?,status=?,analysis=? WHERE id=?', (score, status, json.dumps(data, ensure_ascii=False), p['id']))

    async def publish(self):
        if not self.sender:
            return
        mode = self.store.setting('mode', self.config.mode)
        threshold = self.store.setting('threshold', self.config.threshold)
        threshold = max(90, threshold) if mode == 'critical' else threshold
        disabled = self.store.setting('disabled_categories', [])
        posts = self.store.rows('''SELECT p.*,s.name AS source_name FROM posts p JOIN sources s ON p.source_id=s.id
                                   WHERE p.status IN ('ready','research','sent') AND p.score>=55 ORDER BY p.id LIMIT 200''')
        for p in posts:
            data = json.loads(p['analysis'])
            if data['category'] in disabled:
                continue
            if mode == 'research':
                channel = self.config.research_channel
            elif p['score'] >= threshold:
                channel = self.config.channel
            else:
                channel = self.config.research_channel if p['status'] == 'research' else 0
            if not channel:
                continue
            date = p['published'] or p['discovered']
            if datetime.fromisoformat(date.replace('Z', '+00:00')) < datetime.now(timezone.utc) - timedelta(hours=self.config.max_age):
                continue
            if self.store.rows('SELECT state FROM deliveries WHERE post_id=? AND channel_id=?', (p['id'], channel)):
                continue
            # Persist intent first. A crash during send is marked uncertain, not automatically repeated.
            self.store.execute("INSERT INTO deliveries VALUES(?,?,NULL,'sending',?)", (p['id'], channel, now()))
            try:
                message = await self.sender(channel, p, data)
                self.store.execute("UPDATE deliveries SET message_id=?,state='sent' WHERE post_id=? AND channel_id=?", (message.id, p['id'], channel))
                self.store.execute("UPDATE posts SET status='sent' WHERE id=?", (p['id'],))
            except Exception as exc:
                # Network timeout can mean Discord accepted the message; manual reconciliation required.
                self.store.execute("UPDATE deliveries SET state='uncertain' WHERE post_id=? AND channel_id=?", (p['id'], channel))
                log.error('Delivery %s uncertain: %s', p['id'], exc)

    async def cycle(self):
        async with self.cycle_lock:
            sources = self.store.rows('SELECT * FROM sources WHERE enabled=1 AND next_poll<=?', (time.time(),))
            await asyncio.gather(*(self.poll_source(s) for s in sources))
            await self.process()
            await self.publish()

    async def run(self):
        while True:
            try:
                await self.cycle()
            except Exception:
                log.exception('Monitor cycle failed')
            await asyncio.sleep(self.config.poll)

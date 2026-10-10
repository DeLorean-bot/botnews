import argparse
import asyncio
import json
import logging
import os
import aiohttp
from .config import Config
from .storage import Store
from .service import Service

async def run(args):
    config = Config.load()
    store = Store(config.database)
    try:
        store.seed(config.sources)
        if args.backup:
            store.backup(args.backup)
            print('Backup saved:', args.backup)
            return
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent':'HvHNewsMonitor/0.1 (public news collector)'}) as session:
            service = Service(store, session, config)
            if args.probe:
                sources = store.rows('SELECT * FROM sources WHERE enabled=1')
                from .sources import collect
                report = []
                for source in sources:
                    try:
                        posts = await collect(source, service.fetcher)
                        from .analysis import prefilter
                        report.append({'source': source['name'], 'url': source['url'], 'count': len(posts),
                            'posts': [{'title': p.title, 'url': p.url, 'published': p.published,
                                       'prefilter': prefilter(p.text)} for p in posts]})
                    except Exception as exc:
                        report.append({'source': source['name'], 'url': source['url'], 'error': str(exc)})
                from pathlib import Path
                Path(args.probe).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print('Source probe saved:', args.probe)
            elif args.once or args.publish_once:
                if args.publish_once:
                    if os.getenv('TELEGRAM_BOT_TOKEN'):
                        from .telegram_sender import configure_telegram
                        configure_telegram(service, session)
                    else:
                        if not config.token or not config.channel:
                            raise ValueError('Configure Telegram or Discord delivery before --publish-once')
                        from .discord_bot import embed
                        import discord
                        client = discord.Client(intents=discord.Intents.none(), allowed_mentions=discord.AllowedMentions.none())
                        await client.login(config.token)
                        async def send(channel, post, data):
                            target = await client.fetch_channel(channel)
                            return await target.send(embed=embed(post,data))
                        service.sender = send
                try:
                    await service.cycle()
                finally:
                    if args.publish_once and not os.getenv('TELEGRAM_BOT_TOKEN'):
                        await client.close()
                print(json.dumps(store.rows('SELECT status,count(*) AS count FROM posts GROUP BY status'), ensure_ascii=False))
            elif args.telegram:
                from .telegram_sender import configure_telegram
                configure_telegram(service, session)
                await service.run()
            else:
                if not config.token or not config.channel or not config.admins:
                    raise ValueError('Set DISCORD_TOKEN, DISCORD_CHANNEL_ID and DISCORD_ADMIN_IDS in .env')
                from .discord_bot import NewsBot
                async with NewsBot(config, store, service) as bot:
                    await bot.start(config.token)
    finally:
        store.db.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Collect/analyze once without Discord')
    parser.add_argument('--publish-once', action='store_true', help='One collection/analysis/delivery cycle for scheduled runners')
    parser.add_argument('--telegram', action='store_true', help='Persistent Telegram delivery mode (no Discord commands)')
    parser.add_argument('--probe', metavar='FILE', help='Read real sources and save accessibility/prefilter report')
    parser.add_argument('--backup', metavar='FILE', help='Consistent SQLite backup')
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(name)s %(message)s')
    asyncio.run(run(args))

if __name__ == '__main__':
    main()

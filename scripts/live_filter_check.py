"""Read actual Telegram posts and measure only the preliminary rules, not LLM quality."""
import asyncio
import json
from pathlib import Path
import aiohttp
from botnews.sources import Fetcher, collect
from botnews.analysis import prefilter

async def main():
    report = []
    sources = json.loads(Path('sources.json').read_text(encoding='utf-8'))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        fetcher = Fetcher(session)
        for n, source in enumerate(sources, 1):
            if source['kind'] != 'telegram':
                continue
            posts = await collect(dict(source, id=n), fetcher)
            for post in posts:
                allowed, reason = prefilter(post.text)
                report.append({'source':source['name'], 'url':post.url, 'title':post.title,
                               'allowed':allowed, 'reason':reason})
    Path('live_filter_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Actual posts: {len(report)}; passed preliminary rules: {sum(x["allowed"] for x in report)}')

if __name__ == '__main__':
    asyncio.run(main())

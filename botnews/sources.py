"""Public HTML, RSS/Atom and official GitHub API collectors. No challenge bypass."""
import asyncio
import ipaddress
import re
import socket
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urljoin
import aiohttp
from bs4 import BeautifulSoup
from .models import Post

KINDS = {"telegram", "forum", "rss", "github"}

def validate_source(kind, url):
    if kind not in KINDS:
        raise ValueError(f"Supported: {', '.join(sorted(KINDS))}")
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError("Source must be a public HTTPS URL without credentials")
    if kind == "telegram" and (p.hostname != "t.me" or not re.fullmatch(r"/(?:s/)?[A-Za-z0-9_]+/?", p.path)):
        raise ValueError("Expected https://t.me/channel")
    if kind == "github" and (p.hostname != "github.com" or len(p.path.strip('/').split('/')) != 2):
        raise ValueError("Expected https://github.com/owner/repo")

async def public_host(url):
    host = urlsplit(url).hostname
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    def allowed(address):
        ip = ipaddress.ip_address(address)
        # Explicit support for local TUN/fake-IP routing (198.18.0.0/15), never LAN/loopback.
        fake_dns = os.getenv('ALLOW_FAKE_IP_DNS', 'false').lower() == 'true'
        return ip.is_global or (fake_dns and ip.version == 4 and ip in ipaddress.ip_network('198.18.0.0/15'))
    if any(not allowed(r[4][0]) for r in records):
        raise ValueError("Private/local network sources are forbidden")

class Fetcher:
    def __init__(self, session):
        self.session = session
        self.limit = asyncio.Semaphore(3)
        self.host_locks = {}
        self.host_times = {}

    async def get(self, url, json_response=False):
        # Validate each redirect and pace requests per host, including thread reads.
        async with self.limit:
            for _ in range(5):
                await public_host(url)
                host = urlsplit(url).hostname
                lock = self.host_locks.setdefault(host, asyncio.Lock())
                async with lock:
                    loop = asyncio.get_running_loop()
                    await asyncio.sleep(max(0, self.host_times.get(host, 0) - loop.time()))
                    self.host_times[host] = loop.time() + 1.5
                    async with self.session.get(url, allow_redirects=False) as resp:
                        if resp.status in (301, 302, 303, 307, 308):
                            url = urljoin(url, resp.headers["Location"])
                            if urlsplit(url).scheme != "https":
                                raise ValueError("Non-HTTPS redirect")
                            continue
                        if resp.status == 429:
                            raise RuntimeError(f"Rate limited; Retry-After={resp.headers.get('Retry-After', 'unknown')}")
                        resp.raise_for_status()
                        raw = await resp.content.read(2_000_001)
                        if len(raw) > 2_000_000:
                            raise ValueError("Response exceeds 2 MB")
                        text = raw.decode(resp.charset or "utf-8", errors="replace")
                        if json_response:
                            import json
                            return json.loads(text)
                        if "cf-chl-" in text or "Just a moment..." in text:
                            raise RuntimeError("Access challenge; collector stopped without bypass")
                        return text
            raise RuntimeError("Too many redirects")

def time_value(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else None

def telegram_posts(html, source):
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for item in soup.select(".tgme_widget_message[data-post]"):
        body = item.select_one(".tgme_widget_message_text")
        if not body:
            continue
        text = body.get_text("\n", strip=True)
        date = item.select_one("time[datetime]")
        posts.append(Post(source["id"], "https://t.me/" + item["data-post"], text.split('\n')[0][:240],
                          text, time_value(date.get("datetime")) if date else None,
                          [a["href"] for a in body.select("a[href]") if a["href"].startswith("https://")]))
    return posts

def forum_links(html, url):
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.select(".structItem-title a[href], a[id^='thread_title_']"):
        link = urljoin(url, a["href"])
        if urlsplit(link).hostname == urlsplit(url).hostname and link not in found:
            found.append(link)
    return found[:15]

def forum_post(html, source, url):
    soup = BeautifulSoup(html, "html.parser")
    item = soup.select_one("article.message, .message--post")
    body = item.select_one(".bbWrapper") if item else soup.select_one(".post_message, [id^='post_message_']")
    if not body:
        return None
    for node in body.select("blockquote, .bbCodeBlock--quote, script, style"):
        node.decompose()
    title = soup.select_one("h1") or soup.select_one("title")
    date = item.select_one("time[datetime]") if item else None
    return Post(source["id"], url, title.get_text(' ', strip=True)[:240] if title else "Forum post",
                body.get_text('\n', strip=True), time_value(date.get('datetime')) if date else None,
                [urljoin(url, a['href']) for a in body.select('a[href]') if a['href'].startswith('https://')])

def rss_posts(xml, source):
    root = ET.fromstring(xml)
    posts = []
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    entries = root.findall('.//item') or root.findall('a:entry', ns)
    for e in entries[:30]:
        def value(*names):
            for name in names:
                el = e.find(name, ns)
                if el is not None:
                    return ''.join(el.itertext())
            return ''
        link = value('link')
        if not link:
            el = e.find('a:link', ns)
            link = el.get('href', '') if el is not None else ''
        title = value('title', 'a:title')
        body = BeautifulSoup(value('description', 'a:content', 'a:summary'), 'html.parser').get_text(' ', strip=True)
        if link:
            posts.append(Post(source['id'], link, title, title + '\n' + body,
                              time_value(value('pubDate', 'a:published', 'a:updated'))))
    return posts

async def collect(source, fetcher):
    kind, url = source['kind'], source['url']
    if kind == 'telegram':
        channel = urlsplit(url).path.strip('/').removeprefix('s/')
        posts = telegram_posts(await fetcher.get('https://t.me/s/' + channel), source)
        if not posts:
            raise RuntimeError('Public Telegram preview has no readable text posts')
        return posts
    if kind == 'rss':
        return rss_posts(await fetcher.get(url), source)
    if kind == 'github':
        repo = urlsplit(url).path.strip('/')
        base = 'https://api.github.com/repos/' + repo
        releases = await fetcher.get(base + '/releases?per_page=10', True)
        commits = await fetcher.get(base + '/commits?per_page=10', True)
        issues = await fetcher.get(base + '/issues?state=all&per_page=10', True)
        posts = [Post(source['id'], r['html_url'], r.get('name') or r['tag_name'],
                      (r.get('name') or r['tag_name']) + '\n' + (r.get('body') or ''), r['published_at']) for r in releases]
        posts += [Post(source['id'], r['html_url'], r['commit']['message'].split('\n')[0],
                       r['commit']['message'], r['commit']['committer']['date']) for r in commits]
        posts += [Post(source['id'], r['html_url'], r['title'], r['title'] + '\n' + (r.get('body') or ''),
                       r['created_at']) for r in issues if 'pull_request' not in r]
        return posts
    links = forum_links(await fetcher.get(url), url)
    if not links:
        raise RuntimeError('No supported forum thread markup found (or access denied)')
    posts = []
    for link in links:
        post = forum_post(await fetcher.get(link), source, link)
        if post:
            posts.append(post)
    if not posts:
        raise RuntimeError('No readable first posts found')
    return posts

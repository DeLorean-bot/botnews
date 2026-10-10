"""Telegram Bot API notification transport; no secrets in errors or logs."""
import html
import os
from types import SimpleNamespace

def message_text(post, data):
    esc = lambda s: html.escape(str(s))
    if data.get('official_update'):
        published = post.get('published')
        if published:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            date = datetime.fromisoformat(published.replace('Z', '+00:00')).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M') + ' МСК'
        else:
            date = 'Дата публикации неизвестна'
        size = data.get('download_size') or 'Valve не указала; зависит от платформы и установленной версии'
        changes = '\n'.join('• ' + esc(f['fact'][:240]) for f in data['confirmed'][:8])
        return '\n\n'.join([f"<b>{esc(data['title'][:220])}</b>", esc(date),
             '<b>Размер загрузки обновления:</b> ' + esc(size), esc(data['summary'][:650]),
             '<b>Что изменилось:</b>\n' + (changes or 'Изменения описаны в кратком пересказе выше.'),
             '<a href="' + esc(post['url']) + '">Официальные патчноуты Valve</a>',
             '<a href="https://www.counter-strike.net/news/updates">Обновления Counter-Strike</a>'])
    facts = '\n'.join('• ' + f['fact'] for f in data['confirmed'])
    parts = [f"<b>{esc(data['title'][:220])}</b>",
             f"Категория: {esc(data['category'])} · {post['score']}/100 · Уверенность: {esc(data['confidence'])}",
             esc(data['summary'][:800]), '<b>Почему важно</b>\n' + esc(data['importance'][:550]),
             '<b>Что подтверждено текстом</b>\n' + esc((facts or 'Недостаточно данных')[:550]),
             '<b>Что неизвестно</b>\n' + esc(data['unknown'][:450]),
             '<a href="' + esc(post['url']) + '">Первоначальная публикация</a>']
    for url in data.get('confirmations', [])[:3]:
        parts.append('<a href="' + esc(url) + '">Дополнительный источник</a>')
    parts.append(f"{esc(post['source_name'][:100])} · ID {post['id']} · Пересказ")
    return '\n\n'.join(parts)

class TelegramSender:
    def __init__(self, session, token):
        self.session, self.token = session, token

    async def __call__(self, chat_id, post, data):
        # Never raise aiohttp ClientResponseError: its URL includes the bot token.
        payload = {'chat_id':chat_id, 'text':message_text(post,data), 'parse_mode':'HTML',
                   'link_preview_options':{'is_disabled':True}}
        thread = os.getenv('TELEGRAM_THREAD_ID', '')
        if thread:
            payload['message_thread_id'] = int(thread)
        try:
            async with self.session.post('https://api.telegram.org/bot' + self.token + '/sendMessage',
                json=payload) as response:
                if response.status != 200:
                    raise RuntimeError(f'Telegram delivery HTTP {response.status}')
                result = await response.json()
                if not result.get('ok'):
                    raise RuntimeError('Telegram rejected the message')
                return SimpleNamespace(id=result['result']['message_id'])
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError('Telegram network failure; delivery may be uncertain') from None

def configure_telegram(service, session):
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('TELEGRAM_CHAT_ID', '')
    if not token or not chat:
        raise ValueError('Set TELEGRAM_BOT_TOKEN and numeric TELEGRAM_CHAT_ID')
    service.config.channel = int(chat)
    service.config.research_channel = int(os.getenv('TELEGRAM_RESEARCH_CHAT_ID') or 0)
    service.sender = TelegramSender(session, token)

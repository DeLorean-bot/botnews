"""Telegram Bot API notification transport; no secrets in errors or logs."""
import html
import os
from types import SimpleNamespace

def message_text(post, data):
    esc = lambda s: html.escape(str(s))
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

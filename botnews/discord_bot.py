import asyncio
import json
import logging
from datetime import datetime, timezone
import discord
from discord import app_commands
from .analysis import CATEGORIES, REASONS
from .config import MODES

def safe(text, limit=1024):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text)))[:limit] or 'Не указано'

def embed(post, data):
    if data.get('official_update'):
        result = discord.Embed(title=safe(data['title'], 256),url=post['url'],description=safe(data['summary'], 1000),color=0xE6A23C)
        result.add_field(name='Размер загрузки обновления',value=safe(data.get('download_size') or 'Valve не указала; зависит от платформы и установленной версии'),inline=False)
        result.add_field(name='Что изменилось',value=safe('\n'.join('• '+f['fact'] for f in data['confirmed'][:8])),inline=False)
        result.set_footer(text=f"Valve · ID {post['id']} · Перевод и краткий пересказ")
        return result
    priority = 'HIGH PRIORITY' if post['score'] >= 90 else 'NEWS' if post['score'] >= 75 else 'RESEARCH'
    result = discord.Embed(title=f"[{priority}] {safe(data['title'], 225)}", url=post['url'],
        description=safe(data['summary'], 1000), color=0xE74C3C if post['score'] >= 90 else 0x3498DB)
    result.add_field(name='Категория / важность', value=f"{data['category']} · {post['score']}/100")
    result.add_field(name='Уверенность', value={'low':'Низкая','medium':'Средняя','high':'Высокая'}[data['confidence']])
    result.add_field(name='Почему это важно', value=safe(data['importance'], 850), inline=False)
    facts = '\n'.join('• ' + f['fact'] for f in data['confirmed'])
    result.add_field(name='Что подтверждено текстом источника', value=safe(facts or 'Недостаточно данных', 850), inline=False)
    result.add_field(name='Что неизвестно', value=safe(data['unknown'], 850), inline=False)
    # Only collector/database URLs can appear as citations; model cannot invent links.
    links = [post['url']] + data.get('confirmations', [])
    result.add_field(name='Ссылки', value='\n'.join(f'[Источник {i+1}]({u})' for i,u in enumerate(links))[:1024], inline=False)
    date = post['published']
    label = 'Опубликовано' if date else 'Обнаружено (дата публикации неизвестна)'
    dt = datetime.fromisoformat((date or post['discovered']).replace('Z','+00:00'))
    result.add_field(name=label, value=f'<t:{int(dt.timestamp())}:f>', inline=False)
    result.set_footer(text=f"{post['source_name']} · ID {post['id']} · Пересказ публикации")
    return result

class AdminTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if interaction.user.id not in self.client.config.admins:
            await interaction.response.send_message('Команда доступна только настроенным администраторам.', ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error):
        logging.getLogger(__name__).warning('Command failed: %s', error)
        message = 'Ошибка команды: ' + safe(str(error), 500)
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

class NewsBot(discord.Client):
    def __init__(self, config, store, service):
        super().__init__(intents=discord.Intents.none(), allowed_mentions=discord.AllowedMentions.none())
        self.config, self.store, self.service = config, store, service
        self.tree = AdminTree(self)
        self.monitor = None
        self.register_commands()

    async def setup_hook(self):
        if self.config.guild:
            guild = discord.Object(id=self.config.guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.service.sender = self.send_news
        self.monitor = asyncio.create_task(self.monitor_ready())

    async def monitor_ready(self):
        await self.wait_until_ready()
        await self.service.run()

    async def close(self):
        if self.monitor:
            self.monitor.cancel()
            try:
                await self.monitor
            except asyncio.CancelledError:
                pass
        await super().close()

    async def send_news(self, channel_id, post, data):
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        return await channel.send(embed=embed(post, data))

    def register_commands(self):
        tree, store = self.tree, self.store

        @tree.command(name='sources', description='Источники и состояние сборщиков')
        async def sources(i: discord.Interaction):
            rows = store.rows('SELECT * FROM sources')
            text = '\n'.join(f"{r['id']}: {r['name']} [{r['kind']}] {'ON' if r['enabled'] else 'OFF'} · {r['error'] or r['last_ok'] or 'ещё не проверен'}" for r in rows)
            await i.response.send_message(safe(text, 1900), ephemeral=True)

        @tree.command(name='add_source', description='Добавить публичный источник: telegram, forum, rss, github')
        async def add_source(i: discord.Interaction, name: str, kind: str, url: str, reliability: float = .5):
            store.add_source(name, kind, url, reliability)
            await i.response.send_message('Источник добавлен. Первая проверка создаст исходную базу.', ephemeral=True)

        @tree.command(name='remove_source', description='Отключить источник, сохранив историю')
        async def remove_source(i: discord.Interaction, source_id: int):
            changed = store.execute('UPDATE sources SET enabled=0 WHERE id=?', (source_id,)).rowcount
            await i.response.send_message('Источник отключён.' if changed else 'Источник не найден.', ephemeral=True)

        @tree.command(name='settings', description='Настройки, режим и отключённые категории')
        async def settings(i: discord.Interaction, mode: str | None = None, disabled_categories: str | None = None):
            if mode is not None and mode not in MODES:
                raise ValueError('Режимы: ' + ', '.join(MODES))
            if disabled_categories is not None:
                disabled = [x.strip() for x in disabled_categories.split(',') if x.strip()]
                if not set(disabled) <= CATEGORIES:
                    raise ValueError('Категории: ' + ', '.join(sorted(CATEGORIES)))
                store.set_setting('disabled_categories', disabled)
            if mode:
                store.set_setting('mode', mode)
                store.set_setting('threshold', MODES[mode])
            await i.response.send_message(f"Режим: {store.setting('mode', self.config.mode)}; порог: {store.setting('threshold', self.config.threshold)}; отключены: {store.setting('disabled_categories', [])}", ephemeral=True)

        @tree.command(name='threshold', description='Минимальная оценка публикации 55–100')
        async def threshold(i: discord.Interaction, score: app_commands.Range[int, 55, 100]):
            store.set_setting('threshold', score)
            await i.response.send_message(f'Порог: {score}/100', ephemeral=True)

        async def show(i, query=None):
            if query:
                rows = store.rows('SELECT id,title,score,status,url FROM posts WHERE title LIKE ? OR body LIKE ? ORDER BY id DESC LIMIT 10', ('%' + query + '%', '%' + query + '%'))
            else:
                rows = store.rows("SELECT id,title,score,status,url FROM posts WHERE analysis IS NOT NULL ORDER BY id DESC LIMIT 10")
            text = '\n'.join(f"#{r['id']} {r['score']}/100 [{r['status']}] {r['title'][:100]}\n<{r['url']}>" for r in rows)
            await i.response.send_message(safe(text or 'Ничего не найдено.', 1900), ephemeral=True)

        @tree.command(name='search', description='Поиск в собранных публикациях')
        async def search(i: discord.Interaction, query: str):
            await show(i, query)

        @tree.command(name='latest', description='Последние проанализированные события')
        async def latest(i: discord.Interaction):
            await show(i)

        @tree.command(name='report', description='Статистика за указанное количество дней')
        async def report(i: discord.Interaction, days: app_commands.Range[int, 1, 365] = 7):
            rows = store.rows("SELECT status,count(*) AS n FROM posts WHERE julianday(discovered)>=julianday('now',?) GROUP BY status", (f'-{days} days',))
            uncertain = store.rows("SELECT count(*) AS n FROM deliveries WHERE state IN ('sending','uncertain')")[0]['n']
            await i.response.send_message('\n'.join(f"{r['status']}: {r['n']}" for r in rows) + f'\nНеопределённые отправки: {uncertain}', ephemeral=True)

        @tree.command(name='rescan', description='Повторно поставить публикацию в очередь анализа')
        async def rescan(i: discord.Interaction, post_id: int):
            changed = store.execute("UPDATE posts SET status='pending',attempts=0,next_retry=0,analysis=NULL,duplicate_of=NULL WHERE id=?", (post_id,)).rowcount
            await i.response.send_message('Поставлено в очередь. История отправок сохранена.' if changed else 'Публикация не найдена.', ephemeral=True)

        @tree.command(name='feedback', description='Оценить полезность: useful или reject, причина отклонения')
        async def feedback(i: discord.Interaction, post_id: int, verdict: str, reason: str = 'other'):
            from .models import now
            if verdict not in {'useful', 'reject'} or reason not in REASONS:
                raise ValueError('verdict: useful/reject; reason: ' + ', '.join(sorted(REASONS)))
            rows = store.rows('SELECT analysis FROM posts WHERE id=?', (post_id,))
            if not rows or not rows[0]['analysis']:
                raise ValueError('Проанализированная публикация не найдена')
            category = json.loads(rows[0]['analysis'])['category']
            store.execute('INSERT OR REPLACE INTO feedback VALUES(?,?,?,?,?,?)', (post_id, i.user.id, verdict, reason, category, now()))
            await i.response.send_message('Обратная связь сохранена.', ephemeral=True)

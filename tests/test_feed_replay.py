import importlib.util
from pathlib import Path

def test_replay_labels_archive_and_keeps_original_date():
    spec=importlib.util.spec_from_file_location('test_feed_script',Path(__file__).resolve().parents[1]/'scripts'/'test_feed.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    post={'id':1,'published':'2026-09-15T17:45:32+00:00','url':'https://t.me/neverlosecc/310','score':80,'source_name':'Neverlose'}
    analysis={'title':'Обновление Neverlose','summary':'Технические изменения.','importance':'Изменены функции.',
              'category':'project_update','confidence':'medium','confirmed':[],'unknown':'Проверка не выполнена.'}
    text=module.format_test_message(post,analysis)
    assert 'ТЕСТ' in text and 'архивная' in text
    assert '15.09.2026 20:45 МСК' in text
    assert 'Обновление Neverlose' in text

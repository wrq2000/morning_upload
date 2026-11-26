"""Проверки парсера и месячного цикла. HTML-фикстуры по структуре Альта-Софт.
Все сетевые и почтовые операции заменены локальными заглушками.
"""
import asyncio
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

from common.errors import PublicationPending, SourceError
from processes.export_duty.process import (DOC_INDEX, NEWS_INDEX, ExportDutyProcess,
    parse_document, parse_index, search_document, target_month, official_url, OFFICIAL_INDEX)

DOC = 'https://www.alta.ru/tamdoc/26bn0202/'
NEWS = 'https://www.alta.ru/laws_news/131968/'
TARGET = date(2026, 11, 1)
HTML = '''<html><head><meta charset="utf-8"></head><body>
<h1>Информация Минэкономразвития России от 26.10.2026 "О ставках вывозных таможенных пошлин на нефть и отдельные категории товаров, выработанных из нефти, на период с 1 по 30 ноября 2026 года"</h1>
<table><tr><td>Наименование</td><td>Уровень цены</td></tr><tr><td>Нефть Юралс</td><td>744,6</td></tr></table>
<table><tr><th>Код ТН ВЭД ЕАЭС</th><th>Наименование позиции</th><th>Ставка вывозной таможенной пошлины (в долларах США за 1000 кг)</th></tr>
<tr><td><a>2709&nbsp;00</a></td><td>нефть сырая</td><td>0</td></tr>
<tr><td><a>2711 12</a> – <a>2711 19&nbsp;000 0</a></td><td>сжиженные углеводородные газы</td><td>61,5</td></tr>
<tr><td>2901 10 000 1</td><td>этан, бутан, изобутан</td><td>55,3</td></tr></table></body></html>'''
NEWS_INDEX_HTML = '<h1>Новости законодательства</h1><a href="/laws_news/131968/">Ставки вывозных пошлин на нефть и нефтепродукты с 01.11.2026</a>'
DOC_INDEX_HTML = '<h1>Документы за последний месяц</h1><a href="/tamdoc/26bn0202/">Информация Минэкономразвития России от 26.10.2026 "О ставках вывозных таможенных пошлин на нефть и отдельные категории товаров, выработанных из нефти, на период с 1 по 30 ноября 2026 года"</a>'
NEWS_HTML = '<a href="/tamdoc/26bn0202/">Информации Минэкономразвития от 26.10.2026</a>'
EMPTY_NEWS = '<h1>Новости законодательства</h1>'
EMPTY_DOCS = '<h1>Документы за последний месяц</h1>'

class Response:
    def __init__(self, html):
        self.content = html if isinstance(html, bytes) else ('<meta charset="utf-8">' + html).encode('utf-8')
    def close(self):
        pass

class Client:
    def __init__(self, pages):
        self.pages, self.calls = pages, []
    def get(self, url, **kwargs):
        self.calls.append(url)
        return Response(self.pages[url])
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass

class Clock:
    value = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    @classmethod
    def now(cls, tz):
        return cls.value.astimezone(tz)

class ExportDutyTests(TestCase):
    def test_period(self):
        self.assertEqual(target_month(date(2026, 12, 20)), date(2027, 1, 1))
        self.assertEqual(target_month(date(2027, 1, 19)), date(2027, 1, 1))
        self.assertEqual(target_month(date(2027, 1, 20)), date(2027, 2, 1))
    def test_document(self):
        frame = parse_document(Response(HTML), TARGET, DOC, date(2026, 10, 26))
        self.assertEqual(frame['value'].tolist(), [Decimal('0'), Decimal('61.5')])
        self.assertEqual(frame['document_date'].tolist(), ['2026-10-26'] * 2)
        self.assertEqual(frame['date_to'].tolist(), ['2026-11-30'] * 2)
        for bad in (HTML.replace('61,5', ''), HTML.replace('1000 кг', '1 кг'),
                    HTML.replace('30 ноября', '29 ноября'), HTML.replace('нефть сырая', 'нефть'),
                    HTML.replace('2711 19', '2711 18'), HTML.replace('61,5', '-61,5')):
            with self.assertRaises(SourceError):
                parse_document(Response(bad), TARGET, DOC, date(2026, 10, 26))
        with self.assertRaises(SourceError):
            parse_document(Response(HTML), date(2026, 12, 1), DOC, date(2026, 11, 25))
        with self.assertRaises(SourceError):
            parse_document(Response(HTML), TARGET, DOC, date(2026, 10, 25))
    def test_discovery(self):
        page2 = NEWS_INDEX + 'page_2/'
        pages = {NEWS_INDEX: EMPTY_NEWS + '<a href="/laws_news/page_2/">2</a>',
                 page2: NEWS_INDEX_HTML, NEWS: NEWS_HTML, DOC: HTML}
        c = Client(pages)
        frame = asyncio.run(search_document(c, TARGET, date(2026, 10, 27)))
        self.assertEqual(c.calls, [official_url(TARGET), OFFICIAL_INDEX, NEWS_INDEX, page2, NEWS, DOC])
        self.assertEqual(len(frame), 2)
        c = Client({NEWS_INDEX: EMPTY_NEWS, DOC_INDEX: DOC_INDEX_HTML, DOC: HTML})
        self.assertEqual(len(asyncio.run(search_document(c, TARGET, date(2026, 10, 27)))), 2)
        self.assertEqual(c.calls, [official_url(TARGET), OFFICIAL_INDEX, NEWS_INDEX, DOC_INDEX, DOC])
        c = Client({NEWS_INDEX: EMPTY_NEWS, DOC_INDEX: EMPTY_DOCS})
        with self.assertRaises(PublicationPending):
            asyncio.run(search_document(c, TARGET, date(2026, 10, 25)))
        with self.assertRaises(SourceError):
            parse_index(Response('<h1>Access denied</h1>'), DOC_INDEX, 1, TARGET, 'document')
    def test_official_primary(self):
        url = official_url(TARGET)
        official = HTML.replace('Информация Минэкономразвития России от 26.10.2026 "', '').replace('2026 года"</h1>', '2026 года</h1>')
        official = official.replace('<body>', '<body><time datetime="2026-10-26">26 октября 2026</time>')
        c = Client({url: official})
        frame = asyncio.run(search_document(c, TARGET, date(2026, 11, 25)))
        self.assertEqual(c.calls, [url])
        self.assertEqual(frame['source_url'].tolist(), [url] * 2)
        self.assertEqual(frame['value'].tolist(), [Decimal('0'), Decimal('61.5')])
        self.assertEqual(official_url(date(2028, 2, 1)).split('/')[-1][-26:], 'po_29_fevralya_2028_goda.html'[-26:])
        alt = OFFICIAL_INDEX + 'alternate.html'
        c = Client({url: '<h1>404</h1>', OFFICIAL_INDEX: '<a href="'+alt+'">О ставках вывозных таможенных пошлин на нефть на период с 1 по 30 ноября 2026 года</a>', alt: official})
        frame = asyncio.run(search_document(c, TARGET, date(2026, 11, 25)))
        self.assertEqual(c.calls, [url, OFFICIAL_INDEX, alt])
        self.assertEqual(frame['source_url'].tolist(), [alt]*2)
        c = Client({url: official.replace('61,5', '-61,5'), OFFICIAL_INDEX: ''})
        with self.assertRaises(SourceError):
            asyncio.run(search_document(c, TARGET, date(2026, 11, 25), official_only=True))
        self.assertEqual(c.calls, [url, OFFICIAL_INDEX])

    def test_official_real_november_markup(self):
        # Регрессия: реальный блок статьи из MHTML пользователя, а не макет.
        html = (Path(__file__).parent / "fixtures" / "economy_gov_202611.html").read_text(encoding="utf-8")
        url = official_url(TARGET)
        c = Client({url: html})
        frame = asyncio.run(search_document(c, TARGET, date(2026, 11, 25), official_only=True))
        self.assertEqual(c.calls, [url])
        self.assertEqual(frame['value'].tolist(), [Decimal('0'), Decimal('61.5')])
        self.assertEqual(frame['document_date'].tolist(), ['2026-10-26'] * 2)
        self.assertEqual(frame['source_url'].tolist(), [url] * 2)
        self.assertEqual(frame['product_code'].tolist(), ['CRUDE_OIL', 'LPG'])
        self.assertEqual(frame['date_to'].tolist(), ['2026-11-30'] * 2)
        # Не принимаем неправильный месяц, дату из текста закона или неверную единицу.
        for bad in (html.replace('26 октября 2026 16:00', ''),
                    html.replace('26 октября 2026 16:00', '26 ноября 2026 16:00'),
                    html.replace('30 ноября 2026 года', '31 декабря 2026 года'),
                    html.replace('1000\xa0кг', '1 кг')):
            self.assertNotEqual(bad, html)
            with self.assertRaises(SourceError):
                asyncio.run(search_document(Client({url: bad, OFFICIAL_INDEX: ''}), TARGET,
                                            date(2026, 11, 25), official_only=True))

    def test_official_attachment(self):
        from io import BytesIO
        from openpyxl import Workbook
        url = official_url(TARGET)
        attachment = 'https://economy.gov.ru/material/file/example.xlsx'
        page = '<h1>О ставках вывозных таможенных пошлин на нефть на период с 1 по 30 ноября 2026 года</h1><time datetime="2026-10-26"></time><a href="'+attachment+'">Скачать</a>'
        book = Workbook()
        sheet = book.active
        sheet.append(['Ставки на период с 1 по 30 ноября 2026 года'])
        sheet.append(['Код ТН ВЭД ЕАЭС', 'Наименование', 'Ставка пошлины (в долларах США за 1000 кг)'])
        sheet.append(['2709 00', 'нефть сырая', 0])
        sheet.append(['2711 12 – 2711 19 000 0', 'сжиженные углеводородные газы', 61.5])
        out = BytesIO(); book.save(out); book.close()
        c = Client({url: page, attachment: out.getvalue()})
        frame = asyncio.run(search_document(c, TARGET, date(2026, 11, 25)))
        self.assertEqual(c.calls, [url, attachment])
        self.assertEqual(frame['value'].tolist(), [Decimal('0'), Decimal('61.5')])
        self.assertEqual(frame['source_url'].tolist(), [url]*2)

    def test_official_docx(self):
        from io import BytesIO
        from zipfile import ZipFile
        from xml.sax.saxutils import escape
        url = official_url(TARGET)
        attachment = 'https://economy.gov.ru/material/file/example.docx'
        page = '<h1>О ставках вывозных таможенных пошлин на нефть на период с 1 по 30 ноября 2026 года</h1><a href="'+attachment+'">Скачать</a>'
        def paragraph(text):
            return '<w:p><w:r><w:t>'+escape(text)+'</w:t></w:r></w:p>'
        rows = [['Код ТН ВЭД ЕАЭС', 'Наименование', 'Ставка пошлины (в долларах США за 1000 кг)'], ['2709 00','нефть сырая','0'], ['2711 12 – 2711 19 000 0','сжиженные углеводородные газы','61,5']]
        table = '<w:tbl>'+''.join('<w:tr>'+''.join('<w:tc>'+paragraph(c)+'</w:tc>' for c in row)+'</w:tr>' for row in rows)+'</w:tbl>'
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+paragraph('Информация Минэкономразвития от 26 октября 2026 г.')+paragraph('Ставки на период с 1 по 30 ноября 2026 года')+table+'</w:body></w:document>'
        out=BytesIO()
        with ZipFile(out, 'w') as z:
            z.writestr('word/document.xml', xml)
        c=Client({url:page, attachment:out.getvalue()})
        frame=asyncio.run(search_document(c, TARGET, date(2026, 11, 25)))
        self.assertEqual(frame['value'].tolist(), [Decimal('0'), Decimal('61.5')])
        self.assertEqual(c.calls,[url, attachment])

    def test_state_and_delivery(self):
        import json
        import pandas as pd
        from unittest.mock import AsyncMock
        from processes.export_duty.process import requested_months
        current = date(2026, 11, 1)
        following = date(2026, 12, 1)
        self.assertEqual(requested_months(date(2026, 11, 19)), (current,))
        self.assertEqual(requested_months(date(2026, 11, 20)), (current, following))
        self.assertEqual(requested_months(date(2026, 12, 20)), (following, date(2027, 1, 1)))
        frame = parse_document(Response(HTML), current, DOC, date(2026, 11, 25))
        next_frame = frame.copy()
        next_frame['date'], next_frame['date_to'] = '2026-12-01', '2026-12-31'
        next_frame['document_date'] = '2026-11-26'
        async def collect(client, target, today):
            if target == current:
                return frame
            if target == following and today >= date(2026, 11, 26):
                return next_frame
            raise PublicationPending('ещё нет публикации')
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'EXPORT_DUTY_OUTPUT_DIR': str(Path(tmp) / 'output'),
            'EXPORT_DUTY_STATE_FILE': str(Path(tmp) / 'state.json'),
            'EXPORT_DUTY_SUCCESS_RECIPIENTS': 'test@example.org',
            'EXPORT_DUTY_RUN_DAY': '20',
        }), patch('processes.export_duty.process.datetime', Clock), patch('common.csv_process.datetime', Clock), \
             patch('common.csv_process.send_mail') as send, \
             patch('processes.export_duty.process.search_document', side_effect=collect) as search:
            proc = ExportDutyProcess()
            # Состояние старой версии не препятствует дозагрузке.
            proc.state_file.write_text(json.dumps({'version': 1, 'last_successful_cycle': '2026-11-20'}))
            Clock.value = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
            with patch.object(proc, 'http_client', return_value=Client({})):
                send.return_value = False
                self.assertFalse(proc.run()['success'])
                self.assertEqual(json.loads(proc.state_file.read_text())['version'], 1)
                send.return_value = True
                result = proc.run()
                self.assertTrue(result['success']); self.assertFalse(result['all_success'])
                self.assertTrue(result['pending']); self.assertEqual(result['rows'], 2)
                self.assertEqual(json.loads(proc.state_file.read_text())['sent_months'], ['2026-11-01'])
                result = proc.run()
                self.assertTrue(result['pending']); self.assertEqual(send.call_count, 2)
                Clock.value = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
                result = proc.run()
                self.assertTrue(result['all_success']); self.assertEqual(result['rows'], 2)
                self.assertTrue(proc.run()['skipped'])
                self.assertEqual(send.call_count, 3)
                self.assertEqual(json.loads(proc.state_file.read_text())['sent_months'], ['2026-11-01', '2026-12-01'])
                Clock.value = datetime(2026, 12, 19, 12, tzinfo=timezone.utc)
                self.assertTrue(proc.run()['skipped'])
                # Обе публикации найдены за один запуск — 4 строки.
                Clock.value = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
                proc.state_file.unlink()
                result = proc.run()
                self.assertTrue(result['all_success']); self.assertEqual(result['rows'], 4)
                csv = pd.read_csv(result['csv_file'], sep=';')
                self.assertEqual(list(csv['date']), ['2026-11-01'] * 2 + ['2026-12-01'] * 2)

    def test_archive_fallback(self):
        from processes.export_duty.process import ALL_DOCS_INDEX
        c = Client({NEWS_INDEX: EMPTY_NEWS, DOC_INDEX: EMPTY_DOCS,
                    ALL_DOCS_INDEX: DOC_INDEX_HTML.replace('Документы за последний месяц', 'Все документы'), DOC: HTML})
        frame = asyncio.run(search_document(c, TARGET, date(2026, 11, 28)))
        self.assertEqual(len(frame), 2)
        self.assertIn(ALL_DOCS_INDEX, c.calls)

if __name__ == '__main__':
    main()

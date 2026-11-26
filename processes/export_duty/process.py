"""Ставки Минэкономразвития РФ: нефть и СУГ, официальный сайт, резерв — Альта-Софт.
Проверяем месяц действия, ТН ВЭД ЕАЭС, валюту и единицу измерения.
Python 3.8+ (сам morning_upload использует Python 3.9+).
Обновлено: 25.11.2026, МСК.
"""
import json
import logging
import time
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urljoin, urlsplit
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import pandas as pd
from bs4 import BeautifulSoup

from common.data_gateway import DataGateway
from common.errors import PublicationPending, SourceError
from common.monthly_process import MonthlyCsvMailProcess, latest_cycle
from common.csv_process import CsvMailProcess
from common.storage import run_lock, write_text_atomic

logger = logging.getLogger(__name__)
OFFICIAL_BASE = "https://economy.gov.ru"
OFFICIAL_INDEX = OFFICIAL_BASE + "/material/directions/vneshneekonomicheskaya_deyatelnost/tamozhenno_tarifnoe_regulirovanie/"
OFFICIAL_SLUG = "o_stavkah_vyvoznyh_tamozhennyh_poshlin_na_neft_i_otdelnye_kategorii_tovarov_vyrabotannyh_iz_nefti_na_period_s_1_po_"
MONTH_SLUGS = ("yanvarya", "fevralya", "marta", "aprelya", "maya", "iyunya", "iyulya", "avgusta", "sentyabrya", "oktyabrya", "noyabrya", "dekabrya")
BASE = "https://www.alta.ru"
NEWS_INDEX = BASE + "/laws_news/"
DOC_INDEX = BASE + "/tamdoc/last_month/"
ALL_DOCS_INDEX = BASE + "/tamdoc/all/"
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")
MONTH_RE = "|".join(MONTHS)
PRODUCTS = {
    "270900": ("CRUDE_OIL", "нефть сырая", "2709 00"),
    "271112-2711190000": ("LPG", "сжиженные углеводородные газы", "2711 12 - 2711 19 000 0"),
}
COLUMNS = ("date", "date_to", "product_code", "description", "tnved_code",
           "value", "unit", "document_date", "source_url")
PUBLISHER = re.compile(r"минэкономразвития|министерств[ао] экономического развития")
PERIOD = re.compile(r"период\s+с\s+0?1\s+по\s+(\d{1,2})\s+(" + MONTH_RE + r")\s+(\d{4})\s*(?:года|г\.?)?")
DOC_DATE = re.compile(r"\bот\s+(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
DOC_DATE_WORDS = re.compile(r"\bот\s+(\d{1,2})\s+(" + MONTH_RE + r")\s+(\d{4})\b")


def normalized(value):
    return " ".join(value.replace("ё", "е").lower().split())


def next_month(day):
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def target_month(today, run_day=20):
    return next_month(latest_cycle(today, run_day))


def period_matches(text, target):
    last = monthrange(target.year, target.month)[1]
    return any(int(d) == last and MONTHS[target.month - 1] == m and int(y) == target.year
               for d, m, y in PERIOD.findall(normalized(text)))


def publication_date(text):
    text = normalized(text)
    match = DOC_DATE.search(text)
    if match:
        d, m, y = map(int, match.groups())
        return date(y, m, d)
    match = DOC_DATE_WORDS.search(text)
    if match:
        d, m, y = match.groups()
        return date(int(y), MONTHS.index(m) + 1, int(d))
    raise SourceError("Пошлины: в заголовке нет даты документа Минэкономразвития")


def soup_from(response):
    return BeautifulSoup(response.content, "html.parser")


def safe_url(base, href, kind):
    url = urljoin(base, href)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in ("www.alta.ru", "alta.ru"):
        return None
    pattern = r"/tamdoc/\d{2}[a-z0-9]+/" if kind == "document" else r"/laws_news/\d+/"
    if re.fullmatch(pattern, parts.path, flags=re.I):
        return "https://www.alta.ru" + parts.path
    return None


def news_matches(text, target):
    text = normalized(text)
    if not ("ставк" in text and "вывозн" in text and "пошлин" in text and "нефт" in text):
        return False
    numeric = re.compile(r"(?<!\d)0?1\.0?" + str(target.month) + r"\." + str(target.year) + r"(?!\d)")
    words = re.compile(r"(?<!\d)0?1\s+" + MONTHS[target.month - 1] + r"\s+" + str(target.year) + r"(?!\d)")
    return bool(numeric.search(text) or words.search(text) or period_matches(text, target))


def parse_index(response, index, page, target, kind):
    """Из реальных ссылок списка извлекаем кандидатов и ссылку на следующую страницу."""
    soup = soup_from(response)
    heading = soup.find("h1")
    expected_heading = ("все документы" if index == ALL_DOCS_INDEX else
                        "документы за последний месяц" if kind == "document" else "новости законодательства")
    if heading is None or expected_heading not in normalized(heading.get_text(" ", strip=True)):
        raise SourceError("Пошлины: получена не ожидаемая страница списка " + index)
    links = []
    cutoff = (target - timedelta(days=1)).replace(day=15)
    dates = []
    for anchor in soup.select("a[href]"):
        text = normalized(anchor.get_text(" ", strip=True))
        url = safe_url(index, anchor["href"], kind)
        if not url:
            continue
        if kind == "document":
            if DOC_DATE.search(text) or DOC_DATE_WORDS.search(text):
                try:
                    dates.append(publication_date(text))
                except ValueError:
                    pass
            match = (PUBLISHER.search(text) and "ставках вывозных таможенных пошлин" in text
                     and "нефт" in text and period_matches(text, target))
        else:
            match = news_matches(text, target)
        if match:
            links.append({"url": url, "kind": kind})
    # Архив документов упорядочен по убыванию даты. Новее 15-го числа
    # предыдущего месяца — необходимая область поиска; старее не листаем.
    if not (kind == "document" and dates and min(dates) < cutoff):
        expected = index + "page_" + str(page + 1) + "/"
        if any(urljoin(index, a["href"]).rstrip("/") == expected.rstrip("/")
               for a in soup.select("a[href]")):
            links.append({"url": expected, "kind": "next"})
    return pd.DataFrame(links, columns=("url", "kind")).drop_duplicates()


def parse_news(response):
    """Берём ссылку на полный текст, а не извлекаем ставку из новостного пересказа."""
    soup = soup_from(response)
    links = []
    for a in soup.select("a[href]"):
        text = normalized(a.get_text(" ", strip=True))
        url = safe_url(NEWS_INDEX, a["href"], "document")
        if url and PUBLISHER.search(text):
            links.append({"url": url})
    return pd.DataFrame(links, columns=("url",)).drop_duplicates()


def parse_document(response, target, url, today):
    soup = soup_from(response)
    heading = soup.find("h1")
    title = normalized(heading.get_text(" ", strip=True)) if heading else ""
    if not PUBLISHER.search(title) or "ставках вывозных таможенных пошлин" not in title:
        raise SourceError("Пошлины: не подтверждён заголовок документа Минэкономразвития")
    if not period_matches(title, target):
        raise SourceError("Пошлины: документ относится не к запрошенному месяцу")
    issued = publication_date(title)
    if issued > today or issued < (target - timedelta(days=1)).replace(day=15):
        raise SourceError("Пошлины: дата документа не соответствует периоду или дате проверки")
    return parse_rate_tables(html_tables(soup), target, url, issued)


def html_tables(soup):
    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            if tr.find_parent("table") is not table:
                continue
            cells = [normalized(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"], recursive=False)]
            if cells:
                rows.append(cells)
        tables.append(rows)
    return tables


def parse_rate_tables(tables, target, url, issued):
    last = target.replace(day=monthrange(target.year, target.month)[1])
    found = {}
    for rows in tables:
        if not any("тн вэд" in " ".join(row) and "пошлин" in " ".join(row)
                   and "долларах сша" in " ".join(row) and re.search(r"1\s*000\s*кг", " ".join(row))
                   for row in rows):
            # Заголовок может быть разбит на две строки.
            header = " ".join(" ".join(row) for row in rows[:3])
            if not ("тн вэд" in header and "пошлин" in header and "долларах сша" in header
                    and re.search(r"1\s*000\s*кг", header)):
                continue
        for cells in rows:
            if len(cells) != 3:
                continue
            raw_code, description, raw_value = cells
            code = re.sub(r"\s+", "", raw_code).translate(str.maketrans("–—−", "---"))
            if code not in PRODUCTS:
                continue
            product, expected_description, display_code = PRODUCTS[code]
            if description != expected_description or not re.fullmatch(r"\d+(?:[.,]\d+)?", raw_value):
                raise SourceError("Пошлины: некорректная позиция " + display_code)
            if product in found:
                raise SourceError("Пошлины: повторяющаяся позиция " + display_code)
            found[product] = dict(date=target.isoformat(), date_to=last.isoformat(),
                                  product_code=product, description=expected_description,
                                  tnved_code=display_code, value=Decimal(raw_value.replace(",", ".")),
                                  unit="USD/T", document_date=issued.isoformat(), source_url=url)
    if set(found) != {"CRUDE_OIL", "LPG"}:
        raise SourceError("Пошлины: не найдены обе ставки в таблице с единицей USD за 1000 кг")
    return pd.DataFrame([found["CRUDE_OIL"], found["LPG"]], columns=COLUMNS)


async def fetch_frame(client, url, parser, allow_empty=False):
    return await DataGateway.from_http("export_duty", url, client, parser,
                                       allow_empty=allow_empty).fetch()


# Официальный сайт: адрес формируется по шаблону предоставленной публикации.
# Если slug изменился, ищем ссылку в профильном разделе министерства.
def official_url(target):
    return OFFICIAL_INDEX + OFFICIAL_SLUG + "{}_{}_{}_goda.html".format(
        monthrange(target.year, target.month)[1], MONTH_SLUGS[target.month - 1], target.year)


def official_link(base, href):
    url = urljoin(base, href)
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.netloc in ("economy.gov.ru", "www.economy.gov.ru"):
        return url
    return None


def official_date(text, soup=None):
    # Дата самого документа предпочтительнее даты публикации страницы.
    try:
        return publication_date(text)
    except SourceError:
        pass
    if soup is not None:
        values = []
        for node in soup.select('meta[property="article:published_time"], meta[itemprop="datePublished"], meta[name="datePublished"], time[datetime], [itemprop="datePublished"]'):
            values.append(node.get("content") or node.get("datetime") or node.get_text(" ", strip=True))
        # Не ищем произвольную дату по всей странице: там могут быть даты других новостей.
        for node in soup.select('.e-date_main, .material-date, .material__date, .article-date, .article__date, .publication-date, .news-date, .news__date'):
            values.append(node.get_text(" ", strip=True))
        dates = set()
        for value in values:
            value = normalized(value)
            try:
                if re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[t ])", value):
                    dates.add(date.fromisoformat(value[:10]))
                else:
                    dates.add(publication_date("от " + value))
            except (SourceError, ValueError):
                pass
        if len(dates) == 1:
            return dates.pop()
    raise SourceError("Пошлины: не подтверждена дата официального документа/публикации")


def validate_official_date(issued, target, today):
    if issued > today or issued < (target - timedelta(days=1)).replace(day=15):
        raise SourceError("Пошлины: дата официальной публикации вне ожидаемого периода")


def parse_official_index(response, target):
    soup = soup_from(response)
    links = []
    for a in soup.select("a[href]"):
        url = official_link(OFFICIAL_INDEX, a["href"])
        title = normalized(a.get_text(" ", strip=True))
        if (url and urlsplit(url).path.startswith(urlsplit(OFFICIAL_INDEX).path)
                and urlsplit(url).path.endswith(".html") and "ставках вывозных таможенных пошлин" in title
                and "нефт" in title and period_matches(title, target)):
            links.append({"url": url})
    return pd.DataFrame(links, columns=("url",)).drop_duplicates()


def parse_official_page(response, target, url, today):
    # Подтверждаем официальный домен после возможных редиректов.
    if not official_link(url, getattr(response, "url", url)):
        raise SourceError("Пошлины: официальный запрос перенаправлен на другой домен")
    soup = soup_from(response)
    # Фактическая разметка economy.gov.ru (публикация за ноябрь 2026):
    # section.e-material > header.e-title_main / div.e-date_main.
    # Если основной блок существует, не используем заголовки и таблицы меню,
    # модальных окон и других материалов страницы.
    material = soup.select_one("section.e-material")
    scope = material if material is not None else soup
    headings = [normalized(h.get_text(" ", strip=True))
                for h in scope.select("header.e-title_main, h1, h2, title")]
    if not any("ставках вывозных таможенных пошлин" in h and "нефт" in h and period_matches(h, target) for h in headings):
        raise SourceError("Пошлины: не подтверждён период официальной страницы")
    # Дата документа может находиться в заголовке или в метаданных публикации.
    # Не извлекаем её из ссылок на нормативные акты в тексте статьи.
    date_text = " ".join(headings)
    try:
        issued = official_date(date_text, scope)
        validate_official_date(issued, target, today)
    except SourceError as exc:
        logger.info("Пошлины: дата официальной страницы не подтверждена: %s", exc)
        issued = None
    if issued is not None:
        try:
            frame = parse_rate_tables(html_tables(scope), target, url, issued)
            return pd.DataFrame([{"frame": frame, "url": url, "issued": issued}])
        except SourceError as exc:
            logger.info("Пошлины: таблица на официальной странице не разобрана: %s", exc)
    links = []
    for a in scope.select("a[href]"):
        attachment = official_link(url, a["href"])
        if not attachment:
            continue
        path = urlsplit(attachment).path.lower()
        if path.endswith((".docx", ".xlsx")):
            links.append({"frame": None, "url": attachment, "issued": issued})
    if not links:
        raise SourceError("Пошлины: нет подтверждённой HTML-таблицы или вложения DOCX/XLSX")
    return pd.DataFrame(links, columns=("frame", "url", "issued")).drop_duplicates(subset=["url"])


def parse_official_attachment(response, target, page_url, attachment_url, issued, today):
    if not official_link(attachment_url, getattr(response, "url", attachment_url)):
        raise SourceError("Пошлины: вложение перенаправлено на другой домен")
    suffix = urlsplit(attachment_url).path.lower()
    tables, text = [], []
    if suffix.endswith(".docx"):
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with ZipFile(BytesIO(response.content)) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        def paragraph(node):
            return "".join(t.text or "" for t in node.findall(".//w:t", ns))
        text = [paragraph(node) for node in root.findall(".//w:p", ns)]
        for table in root.findall(".//w:tbl", ns):
            tables.append([[normalized(" ".join(paragraph(p) for p in cell.findall(".//w:p", ns)))
                            for cell in row.findall("w:tc", ns)] for row in table.findall("w:tr", ns)])
    elif suffix.endswith(".xlsx"):
        from openpyxl import load_workbook
        book = load_workbook(BytesIO(response.content), read_only=True, data_only=True)
        try:
            for sheet in book:
                rows = []
                for row in sheet.iter_rows(values_only=True):
                    cells = [normalized(str(v)) if v is not None else "" for v in row]
                    while cells and not cells[-1]:
                        cells.pop()
                    if cells:
                        rows.append(cells)
                        text.extend(cells)
                tables.append(rows)
        finally:
            book.close()
    else:
        raise SourceError("Пошлины: неподдерживаемый формат официального вложения")
    full_text = " ".join(text)
    if not period_matches(full_text, target):
        raise SourceError("Пошлины: период не подтверждён внутри официального вложения")
    # Ищем только дату в начале документа, до нормативных ссылок.
    preamble = re.split(r"в соответствии|на основании", full_text, maxsplit=1, flags=re.I)[0]
    try:
        document_date = official_date(preamble)
    except SourceError:
        document_date = issued
    if document_date is None:
        raise SourceError("Пошлины: нет подтверждённой даты официального вложения")
    validate_official_date(document_date, target, today)
    return parse_rate_tables(tables, target, page_url, document_date)


async def search_official(client, target, today):
    attempted = set()
    errors = []
    async def load(url):
        if url in attempted:
            return None
        attempted.add(url)
        try:
            result = await fetch_frame(client, url, lambda r: parse_official_page(r, target, url, today))
            for row in result.itertuples(index=False):
                if isinstance(row.frame, pd.DataFrame):
                    return row.frame
                try:
                    return await fetch_frame(client, row.url, lambda r: parse_official_attachment(
                        r, target, url, row.url, row.issued, today))
                except Exception as exc:
                    errors.append(str(exc))
                    logger.warning("Пошлины: официальное вложение %s: %s", row.url, exc)
        except Exception as exc:
            errors.append(str(exc))
            logger.warning("Пошлины: официальный документ %s: %s", url, exc)
        return None
    frame = await load(official_url(target))
    if frame is not None:
        return frame
    try:
        links = await fetch_frame(client, OFFICIAL_INDEX, lambda r: parse_official_index(r, target), allow_empty=True)
        for url in links["url"]:
            frame = await load(url)
            if frame is not None:
                return frame
    except Exception as exc:
        errors.append(str(exc))
    raise SourceError("Минэкономразвития: ставки на {} не подтверждены: {}".format(target.strftime("%Y-%m"), "; ".join(errors[-3:])))


async def search_document(client, target, today, official_only=False):
    logger.info("Пошлины: основной источник — Минэкономразвития, %s", target.strftime("%Y-%m"))
    try:
        frame = await search_official(client, target, today)
        logger.info("Пошлины: подтверждены официальным источником, %s", target.strftime("%Y-%m"))
        return frame
    except SourceError as exc:
        if official_only:
            raise
        logger.warning("Пошлины: %s; переключение на резерв Альта-Софт", exc)
    return await search_alta(client, target, today)


async def search_alta(client, target, today):
    """Два пути поиска: документы за последний месяц и новости со ссылкой на документ."""
    attempted = set()
    errors = []
    candidate_found = False
    async def load_document(url):
        nonlocal candidate_found
        if url in attempted:
            return None
        attempted.add(url)
        candidate_found = True
        try:
            return await fetch_frame(client, url, lambda r: parse_document(r, target, url, today))
        except Exception as exc:
            errors.append(str(exc))
            logger.warning("Пошлины: документ %s не разобран: %s", url, exc)
            return None

    # Новости дешевле в опросе, архив документов — независимо от наличия новости.
    indexes = [(NEWS_INDEX, "news", 3), (DOC_INDEX, "document", 15)]
    # Текущая ставка могла быть опубликована более месяца назад.
    if target <= today.replace(day=1) and (today - (target - timedelta(days=1)).replace(day=15)).days > 30:
        indexes.append((ALL_DOCS_INDEX, "document", 40))
    for index, kind, limit in indexes:
        url = index
        for page in range(1, limit + 1):
            try:
                links = await fetch_frame(client, url,
                    lambda r: parse_index(r, index, page, target, kind), allow_empty=True)
            except Exception as exc:
                errors.append(str(exc))
                logger.warning("Пошлины: список %s не прочитан: %s", url, exc)
                break
            for candidate in links.loc[links["kind"] == kind, "url"]:
                if kind == "news":
                    try:
                        docs = await fetch_frame(client, candidate, parse_news, allow_empty=True)
                        if docs.empty:
                            errors.append("Нет ссылки на документ в " + candidate)
                        doc_urls = docs["url"].tolist()
                    except Exception as exc:
                        errors.append(str(exc))
                        continue
                else:
                    doc_urls = [candidate]
                for doc_url in doc_urls:
                    frame = await load_document(doc_url)
                    if frame is not None:
                        return frame
            next_urls = links.loc[links["kind"] == "next", "url"]
            if next_urls.empty:
                break
            url = next_urls.iloc[0]
    message = "Пошлины: ставки на {} ещё не найдены".format(target.strftime("%Y-%m"))
    if candidate_found or errors:
        raise SourceError(message + "; ошибки поиска/разбора: " + "; ".join(errors[-3:]))
    raise PublicationPending(message + "; повтор при следующем запуске")


def requested_months(today, run_day=20):
    """Текущий месяц всегда; следующий — с начала очередного цикла."""
    current = today.replace(day=1)
    if latest_cycle(today, run_day).month == today.month:
        return (current, next_month(current))
    return (current,)


def log_rates(frame):
    for row in frame.itertuples(index=False):
        logger.info("Пошлины: %s | %s | %s %s | документ %s | %s",
                    row.date, row.product_code, row.value, row.unit,
                    row.document_date, row.source_url)


def report_body(frame, unavailable):
    lines = ["Ставки вывозных таможенных пошлин Минэкономразвития России."]
    for _, group in frame.groupby("date", sort=True):
        first = group.iloc[0]
        values = dict(zip(group.product_code, group.value))
        lines.extend(["", "Период: {} — {}. Документ от {}.".format(
            first.date, first.date_to, first.document_date),
            "Нефть сырая: {} USD/т.".format(values["CRUDE_OIL"]),
            "СУГ: {} USD/т.".format(values["LPG"]),
            "Источник: " + first.source_url])
    if unavailable:
        lines.extend(["", "Не получены (повтор при следующем запуске):"] + unavailable)
    return "\n".join(lines)


async def collect_months(client, months, today, official_only=False):
    """Каждый месяц проверяется независимо. Ошибка не уничтожает другой результат."""
    frames, pending, errors = [], [], []
    for month in months:
        try:
            frame = await search_document(client, month, today, official_only=True) if official_only else await search_document(client, month, today)
            log_rates(frame)
            frames.append(frame)
        except PublicationPending as exc:
            if month <= today.replace(day=1):
                errors.append(str(exc))
                logger.warning("Пошлины: не найден уже действующий месяц %s", month)
            else:
                pending.append(str(exc))
                logger.info("%s", exc)
        except SourceError as exc:
            errors.append(str(exc))
            logger.warning("%s", exc)
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    return frame, pending, errors


class ExportDutyProcess(MonthlyCsvMailProcess):
    def __init__(self):
        super().__init__("EXPORT_DUTY", "Вывозные пошлины — нефть и СУГ",
                         "export_duty.csv", default_run_day=20)

    async def collect(self, client, today):
        months = getattr(self, "_missing_months", None) or requested_months(today, self.run_day)
        frame, pending, errors = await collect_months(client, months, today)
        self._pending, self._errors = pending, errors
        if frame.empty:
            if errors:
                raise SourceError("; ".join(errors + pending))
            raise PublicationPending("; ".join(pending))
        self._collected_months = set(frame["date"])
        return frame, report_body(frame, errors + pending)

    def run(self):
        """Тот же месячный цикл, но с отметкой об отправке каждого месяца.

        Версия 1 не доказывает отправку текущего месяца: один раз перепроверяем
        оба периода. Общий MonthlyCsvMailProcess для Eurostat не изменён.
        """
        started = time.monotonic()
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with run_lock(self.state_file.with_name(self.state_file.name + ".lock")):
                today = datetime.now(self.timezone).date()
                cycle = latest_cycle(today, self.run_day).isoformat()
                required = set(m.isoformat() for m in requested_months(today, self.run_day))
                sent = set()
                if self.state_file.exists():
                    state = json.loads(self.state_file.read_text(encoding="utf-8"))
                    if not isinstance(state, dict) or state.get("version") not in (1, 2):
                        raise ValueError("Неверный формат состояния " + str(self.state_file))
                    if state["version"] == 2:
                        if not isinstance(state.get("sent_months"), list):
                            raise ValueError("Неверный список отправленных месяцев")
                        if state["cycle"] == cycle:
                            sent = {date.fromisoformat(m).isoformat() for m in state["sent_months"]}
                    else:
                        logger.info("Пошлины: состояние v1, однократная повторная проверка")
                missing = required - sent
                if not missing:
                    logger.info("Пошлины: все необходимые месяцы уже отправлены, цикл %s", cycle)
                    return {"success": True, "all_success": True, "skipped": True,
                            "cycle": cycle, "errors": [], "duration_sec": round(time.monotonic() - started, 2)}
                self._missing_months = tuple(date.fromisoformat(m) for m in sorted(missing))
                self._collected_months, self._pending, self._errors = set(), [], []
                try:
                    result = CsvMailProcess.run(self)
                finally:
                    self._missing_months = None
                result["cycle"] = cycle
                if result.get("all_success", result.get("success", False)):
                    sent.update(self._collected_months)
                    complete = required <= sent
                    state = {"version": 2, "cycle": cycle, "sent_months": sorted(sent),
                             "completed_at": datetime.now(self.timezone).isoformat(timespec="seconds"),
                             "csv_file": result["csv_file"], "rows": result["rows"]}
                    if complete:
                        state["last_successful_cycle"] = cycle
                    write_text_atomic(self.state_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
                    self.state_file.chmod(0o644)
                    result.update(all_success=complete, pending=bool(self._pending),
                                  errors=self._errors + self._pending, partial=not complete)
                    logger.info("Пошлины: отправлены месяцы %s; осталось %s", sorted(sent), sorted(required - sent))
                result["duration_sec"] = round(time.monotonic() - started, 2)
                return result
        except Exception as exc:
            logger.exception("Пошлины: ежемесячный процесс не завершён")
            return {"success": False, "all_success": False, "errors": [str(exc)],
                    "duration_sec": round(time.monotonic() - started, 2)}

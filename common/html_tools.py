"""Выбор ссылок и текста внутри блока HTML. Python 3.8+. Обновлено 17.11.2026."""
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from .errors import SourceError
from .text import clean_text

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}


class PageLinks(HTMLParser):
    """Ссылки и текст только внутри нужного блока, без меню и подвала."""

    def __init__(self, *, element_id: str = "", element_class: str = "",
                 element_attrs: Optional[Dict[str, str]] = None):
        super().__init__(convert_charrefs=True)
        self.element_id = element_id
        self.element_class = element_class
        self.element_attrs = dict(element_attrs or {})
        self.title_parts = []  # type: List[str]
        self.stack = []  # type: List[Tuple[str, bool]]
        self.scope_count = 0
        self.parts = []  # type: List[str]
        self.links = []  # type: List[Dict[str, str]]
        self.active_link = None  # type: Optional[Dict[str, str]]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        begins = ((bool(self.element_id) and attrs.get("id") == self.element_id)
                  or (bool(self.element_class)
                      and self.element_class in (attrs.get("class") or "").split())
                  or (bool(self.element_attrs)
                      and all(attrs.get(key) == value for key, value in self.element_attrs.items())))
        inside = begins or bool(self.stack and self.stack[-1][1])
        if begins:
            self.scope_count += 1
        if tag == "a" and inside:
            self.active_link = {"href": attrs.get("href", ""), "text": ""}
        if tag not in VOID_TAGS:
            self.stack.append((tag, inside))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.stack and self.stack[-1][0] == "title":
            self.title_parts.append(data)
        if any(tag in {"script", "style", "template"} for tag, _ in self.stack):
            return
        if self.stack and self.stack[-1][1]:
            self.parts.append(data)
            if self.active_link is not None:
                self.active_link["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.active_link is not None:
            self.links.append(self.active_link)
            self.active_link = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    @property
    def text(self):
        # Номер сессии может быть разбит на несколько соседних <b>.
        # Между такими фрагментами нельзя добавлять отсутствующие пробелы.
        return clean_text("".join(self.parts))



def parse_page(text: str, *, fallbacks=(), **kwargs) -> PageLinks:
    """Выбрать один блок; новые селекторы проверяются до старой разметки.

    Next.js может отдавать содержимое в HTML-фрагменте вне main-content,
    а затем перемещать его JavaScript. Ищем сам блок данных во всём HTML,
    не исполняя скрипты и не принимая их текст за содержимое страницы.
    Неоднозначный селектор не заменяется более широким fallback.
    """
    checked = []
    title = ""
    for selector in (kwargs,) + tuple(fallbacks):
        parser = PageLinks(**selector)
        parser.feed(text)
        parser.close()
        title = clean_text("".join(parser.title_parts))[:160] or title
        checked.append("{}: {}".format(selector, parser.scope_count))
        if parser.scope_count == 1:
            if any(inside for _, inside in parser.stack):
                raise SourceError("Получен неполный блок данных страницы: {}. "
                                  "Повторите загрузку после проверки сети".format(selector))
            return parser
        if parser.scope_count > 1:
            break
    raise SourceError(
        "Не найден единственный блок данных страницы. "
        "Возможны изменение сайта или страница ограничения доступа. "
        "Проверены селекторы: {}. Заголовок: {}".format("; ".join(checked), title or "отсутствует"))


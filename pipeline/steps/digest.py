"""The digest (stage `digest`): the period's most covered events, each in a sentence or two, and a rough forecast for
gold, Brent and WTI. Written in Russian by the writer model from verified facts; the layout, links and labels are ours,
so the model never writes markup or URLs."""
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Literal

from pydantic import BaseModel, Field

from .common import ModelStep

Direction = Literal['up', 'down', 'mixed', 'flat']

DIGEST_SYSTEM = """You write a short markets digest in Russian for a Telegram channel, from the events given.
Each event comes with its category, how many sites reported it, and its verified facts.
- overview: 1 to 3 sentences on the main theme of the period: what mattered most and why.
- items: at most {max_items}, most important for markets first. Several events can be the same story (one IPO,
  one central bank decision told from different angles): write ONE item for them and list all their numbers in
  events. Leave out what matters least for markets when there are more stories than items.
  events: the numbers of the events the item tells, the main one first.
  headline: up to 80 characters, the event itself (who did what), no clickbait.
  text: 1 or 2 sentences with the key numbers and the consequence the facts state.
  Use only the facts of those events: no numbers, names or dates that are not in them.
- forecast: for each of gold, Brent crude and WTI crude, where these events push it in the coming days.
  direction: up; down; mixed when the events pull both ways; flat when nothing in them moves it much.
  reason: one sentence naming the events and the usual market logic, for example: a rate hike by the Fed makes
  the dollar dearer and gold cheaper; an OPEC+ cut or a supply disruption lifts oil. Brent and WTI usually move
  together: if you see them apart, say why (US inventories and output weigh more on WTI, Middle East and
  Russian supply on Brent).
  It is a rough estimate from the news, not advice: no price targets, no certainty.
The reader never sees the event numbers: never write them in overview, text or reason ("событие 4" is wrong);
name the event instead (решение ФРС, рост запасов нефти в США).
Write natural Russian: company and institution names as Russian media write them (ФРС, ЕЦБ, ОПЕК+, Nvidia),
numbers with their units ($85,2 за баррель, 4,5%). Plain text: no markdown, no emoji, no links.
Answer with JSON only."""


class DigestItem(BaseModel):
    events: list[int] = Field(min_length=1)
    headline: str
    text: str


class Move(BaseModel):
    direction: Direction
    reason: str


class Forecast(BaseModel):
    gold: Move
    brent: Move
    wti: Move


class DigestAnswer(BaseModel):
    overview: str
    items: list[DigestItem]
    forecast: Forecast


@dataclass(frozen=True)
class EventBrief:
    """One event as the writer sees it, and where the reader can read more."""

    number: int
    category: str
    sites: int
    facts: list[str]
    link_url: str
    link_name: str


def digest_prompt(events: list[EventBrief]) -> str:
    blocks = []
    for event in events:
        facts = '\n'.join(f'- {fact}' for fact in event.facts)
        blocks.append(f'Event {event.number} ({event.category}, {event.sites} sites):\n{facts}')
    return '\n\n'.join(blocks)


class DigestWriter(ModelStep):
    max_tokens = 4096  # 12 items and a forecast in Russian: ~2000 tokens, plus the JSON around them

    def write(self, events: list[EventBrief], max_items: int) -> DigestAnswer:
        return self.ask(DIGEST_SYSTEM.format(max_items=max_items), digest_prompt(events), DigestAnswer).value


# --- layout ------------------------------------------------------------------------------------------------------

CATEGORIES = [('macro', 'Макро и ставки'), ('markets', 'Рынки'), ('energy', 'Нефть и газ'), ('metals', 'Металлы')]
ASSETS = [('gold', 'Золото'), ('brent', 'Нефть Brent'), ('wti', 'Нефть WTI')]  # always these, in this order
ARROWS = {'up': '🟢 ↑', 'down': '🔴 ↓', 'mixed': '🟡 ↕', 'flat': '⚪ →'}
MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября',
          'декабря']
DISCLAIMER = 'Примерная оценка по новостям, не инвестиционная рекомендация.'
MESSAGE_LIMIT = 4096  # Telegram's limit for one message, in characters


def digest_title(moment: datetime) -> str:
    """moment: local time. Before 15:00 it is the morning digest."""
    part = 'утро' if moment.hour < 15 else 'вечер'
    return f'Сводка рынков · {moment.day} {MONTHS[moment.month - 1]}, {part}'


def kept_items(answer: DigestAnswer, events: dict[int, EventBrief], max_items: int) -> list[DigestItem]:
    """The writer's items as they go out: event numbers it made up or already used in an earlier item are dropped,
    then items left with no event, then all past max_items."""
    used, items = set(), []
    for item in answer.items:
        numbers = [n for n in dict.fromkeys(item.events) if n in events and n not in used]
        if numbers:
            used.update(numbers)
            items.append(item.model_copy(update={'events': numbers}))
    return items[:max_items]


EVENT_REF = re.compile(r'\s*\((?:событи[еяйю]|event)\s*№?\s*\d+(?:\s*(?:,|и|and)\s*\d+)*\)'
                       r'|(?:событи[еяйю]|event)\s*№?\s*\d+', re.IGNORECASE)


def clean(text: str) -> str:
    """The writer's text without the event numbers it was told never to write ("(событие 4)")."""
    return re.sub(r' {2,}', ' ', EVENT_REF.sub('', text)).replace(' ,', ',').replace(' .', '.').strip()


def render(title: str, answer: DigestAnswer, events: dict[int, EventBrief], max_items: int = 12) -> list[str]:
    """Telegram HTML: title and overview, the items by category with a link to a source, the forecast.
    An item goes under the category of its main event. Split into messages under Telegram's limit, by block."""
    blocks = [f'<b>{escape(title)}</b>\n\n{escape(clean(answer.overview))}']
    items = kept_items(answer, events, max_items)
    for category, label in CATEGORIES:
        lines = [_item(item, events) for item in items if events[item.events[0]].category == category]
        if lines:
            blocks.append(f'<b>{label}</b>\n' + '\n\n'.join(lines))
    moves = '\n'.join(_move(label, getattr(answer.forecast, asset)) for asset, label in ASSETS)
    blocks.append(f'<b>Прогноз</b>\n{moves}\n\n<i>{DISCLAIMER}</i>')
    return split(blocks)


def _item(item: DigestItem, events: dict[int, EventBrief]) -> str:
    """The link goes to the most covered of the item's events; +N: the other sites that reported it."""
    event = max((events[n] for n in item.events), key=lambda e: e.sites)
    more = f' +{event.sites - 1}' if event.sites > 1 else ''
    link = f'<a href="{escape(event.link_url, quote=True)}">{escape(event.link_name)}</a>{more}'
    return f'• <b>{escape(clean(item.headline))}</b>. {escape(clean(item.text))} ({link})'


def _move(label: str, move: Move) -> str:
    return f'{ARROWS[move.direction]} <b>{label}</b> — {escape(clean(move.reason))}'


def split(blocks: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    messages, current = [], ''
    for block in blocks:
        joined = f'{current}\n\n{block}' if current else block
        if len(joined) <= limit:
            current = joined
            continue
        if current:
            messages.append(current)
        current = block
        while len(current) > limit:  # one category alone over the limit: cut at its last item that fits
            cut = current.rfind('\n\n', 0, limit)
            cut = cut if cut > 0 else limit
            messages.append(current[:cut])
            current = current[cut:].lstrip('\n')
    if current:
        messages.append(current)
    return messages

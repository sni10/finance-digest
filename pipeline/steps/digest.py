"""The digest (stage `digest`): the period's most covered events, each in a sentence or two, and a rough forecast for
gold, Brent and WTI. Written in Russian by the writer model from verified facts; the layout, links and labels are ours,
so the model never writes markup or URLs."""
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Literal

from pydantic import BaseModel

from .common import ModelStep

Direction = Literal['up', 'down', 'mixed', 'flat']

DIGEST_SYSTEM = """You write a short markets digest in Russian for a Telegram channel, from the events given.
Each event comes with its category, how many sites reported it, and its verified facts.
- overview: 1 to 3 sentences on the main theme of the period: what mattered most and why.
- items: one per event given, most important first; event = the event's number.
  headline: up to 80 characters, the event itself (who did what), no clickbait.
  text: 1 or 2 sentences with the key numbers and the consequence the facts state.
  Use only the facts of that event: no numbers, names or dates that are not in them.
- forecast: for each of gold, Brent crude and WTI crude, where these events push it in the coming days.
  direction: up; down; mixed when the events pull both ways; flat when nothing in them moves it much.
  reason: one sentence naming the events and the usual market logic, for example: a rate hike by the Fed makes
  the dollar dearer and gold cheaper; an OPEC+ cut or a supply disruption lifts oil. Brent and WTI usually move
  together: if you see them apart, say why (US inventories and output weigh more on WTI, Middle East and
  Russian supply on Brent).
  It is a rough estimate from the news, not advice: no price targets, no certainty.
Write natural Russian: company and institution names as Russian media write them (ФРС, ЕЦБ, ОПЕК+, Nvidia),
numbers with their units ($85,2 за баррель, 4,5%). Plain text: no markdown, no emoji, no links.
Answer with JSON only."""


class DigestItem(BaseModel):
    event: int
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

    def write(self, events: list[EventBrief]) -> DigestAnswer:
        return self.ask(DIGEST_SYSTEM, digest_prompt(events), DigestAnswer).value


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


def render(title: str, answer: DigestAnswer, events: dict[int, EventBrief]) -> list[str]:
    """Telegram HTML: title and overview, the items by category with a link to a source, the forecast.
    Items of events the writer made up are dropped. Split into messages under Telegram's limit, by block."""
    blocks = [f'<b>{escape(title)}</b>\n\n{escape(answer.overview.strip())}']
    items = [item for item in answer.items if item.event in events]
    for category, label in CATEGORIES:
        lines = [_item(item, events[item.event]) for item in items if events[item.event].category == category]
        if lines:
            blocks.append(f'<b>{label}</b>\n' + '\n\n'.join(lines))
    moves = '\n'.join(_move(label, getattr(answer.forecast, asset)) for asset, label in ASSETS)
    blocks.append(f'<b>Прогноз</b>\n{moves}\n\n<i>{DISCLAIMER}</i>')
    return split(blocks)


def _item(item: DigestItem, event: EventBrief) -> str:
    more = f' +{event.sites - 1}' if event.sites > 1 else ''
    link = f'<a href="{escape(event.link_url, quote=True)}">{escape(event.link_name)}</a>{more}'
    return f'• <b>{escape(item.headline.strip())}</b>. {escape(item.text.strip())} ({link})'


def _move(label: str, move: Move) -> str:
    return f'{ARROWS[move.direction]} <b>{label}</b> — {escape(move.reason.strip())}'


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

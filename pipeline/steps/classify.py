from typing import Literal

from pydantic import BaseModel

from sources.models import Article

from .common import ModelStep, article_block

CATEGORIES = """Categories:
- macro: central banks and interest rates, inflation, jobs, GDP and other economic data, government budgets and debt,
  tariffs and trade, sanctions and geopolitics that move economies
- markets: stock indexes and shares, company earnings, bonds and yields, currencies, funds, market regulation
- metals: gold, silver, platinum, copper and other metals, miners and their output
- energy: crude oil, natural gas, fuels, OPEC, refining, pipelines and tankers, power prices
- other: anything else (not finance or economy news)
If an article fits several categories, pick the one it is mainly about: a Fed decision is "macro" even when the
article covers the market reaction; an oil price move is "energy"."""

ARTICLE_SYSTEM = f"""You classify finance and economy news articles for a daily markets digest.
{CATEGORIES}
Answer with JSON only."""

EVENT_SYSTEM = f"""You classify one news event, given the articles that report it, for a daily markets digest.
{CATEGORIES}
high_risk: true if the event rests on unnamed sources or rumours ("people familiar", "sources say"); is about fraud,
charges, lawsuits or fines; or is a market-moving claim not yet confirmed by an official body. Such news needs two
independent sources before it goes into the digest.
Answer with JSON only."""

Category = Literal['macro', 'markets', 'metals', 'energy', 'other']


class ArticleAnswer(BaseModel):
    category: Category


class EventAnswer(BaseModel):
    category: Category
    high_risk: bool


class EventClassifier(ModelStep):
    max_tokens = 64
    CHARS = 1500  # of the first article; the others by title only

    def classify(self, articles: list[Article]) -> EventAnswer:
        first, *others = articles
        titles = '\n'.join(f'- {article.source.name}: {article.title}' for article in others)
        user = article_block(first, self.CHARS) + (f'\n\nOther articles on the event:\n{titles}' if titles else '')
        return self.ask(EVENT_SYSTEM, user, EventAnswer).value

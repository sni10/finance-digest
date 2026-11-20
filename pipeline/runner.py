"""The pipeline: new articles -> events -> facts -> digest -> Telegram.

Phases by model, so each big model is loaded once per run:
- worker (9B + embeddings): group articles into events; classify ready events; extract and verify their facts;
- writer (27B): one digest of the period's most covered events with verified facts, and a rough forecast for
  gold, Brent and WTI;
- no model: unsent digests to the Telegram channel.
Every step keeps its result in the DB: a run that stops halfway is continued by the next one.
"""
from collections.abc import Callable
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from llm.client import LLMClient, LLMError

from .models import Digest, Event, Fact, PipelineRun
from .models_manager import LoadSpec, LmsModelManager, NoModelManager
from .steps.classify import EventClassifier
from .steps.digest import ASSETS, DigestWriter, EventBrief, digest_title, kept_items, render
from .steps.facts import FactExtractor, FactVerifier, check_facts
from .steps.grouping import Grouper, ungrouped_articles
from .telegram import Telegram, TelegramError

STAGES = ('group', 'facts', 'digest', 'send')
MIN_INDEPENDENT_SOURCES = 2  # for high-risk events: rumours, unnamed sources, fraud, charges, fines


class Pipeline:
    def __init__(self, client: LLMClient, lm_studio, config, manager: LmsModelManager | NoModelManager,
                 progress: Callable[[str], None] = print, digest=None, telegram=None,
                 events: list[int] | None = None):
        self.client = client
        self.only = events  # facts again for just these events, whatever their status (pipeline --event)
        self.digest_config = digest or settings.DIGEST
        self.telegram = telegram or settings.TELEGRAM
        self.config = config
        self.progress = progress
        self.manager = manager
        self.worker_model = lm_studio.worker_model
        self.writer_model = lm_studio.writer_model or lm_studio.worker_model
        self.embedding_model = lm_studio.embedding_model
        self.worker = (LoadSpec(self.worker_model, config.worker_context), LoadSpec(self.embedding_model))
        self.writer = (LoadSpec(self.writer_model, config.writer_context, config.writer_gpu),)
        self.window = timedelta(days=config.window_days)

    def run(self, stages=STAGES) -> PipelineRun:
        run = PipelineRun.objects.create()
        try:
            for stage in STAGES:
                if stage in stages:
                    self.progress(f'== {stage}')
                    run.stats[stage] = getattr(self, stage)()
                    run.save(update_fields=['stats'])
        except Exception as exc:
            run.error = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            run.finished_at = timezone.now()
            run.save(update_fields=['finished_at', 'stats', 'error'])
        return run

    # --- worker: events --------------------------------------------------------------------------------------

    def group(self) -> dict:
        if self.only:
            return {'articles': 0, 'skipped': 'chosen events only'}
        articles = ungrouped_articles(timezone.now() - self.window)
        self.progress(f'{len(articles)} new articles')
        if not articles:
            return {'articles': 0}
        self.manager.use(*self.worker)
        grouper = Grouper(self.client, self.worker_model, self.embedding_model, self.config.group_threshold,
                          self.window)
        stats = grouper.group(articles, self.progress)
        return {'articles': stats.articles, 'embedded': stats.embedded, 'attached': stats.attached,
                'new_events': stats.new_events, 'model_checks': stats.asked, 'rejected': stats.rejected[:50]}

    def ready_events(self) -> list[Event]:
        """Waiting events that may get facts now: two sources, or one that waited long enough for others.
        The most covered first: they matter most and are the safest."""
        sources = Count('articles__source', distinct=True)
        if self.only:
            return list(Event.objects.filter(pk__in=self.only).annotate(source_count=sources)
                        .prefetch_related('articles__source'))
        now = timezone.now()
        events = (Event.objects.filter(status=Event.Status.COLLECTING, last_article_at__gte=now - self.window)
                  .annotate(source_count=sources)
                  .prefetch_related('articles__source').order_by('-source_count', '-last_article_at'))
        wait = now - timedelta(hours=self.config.single_source_wait_hours)
        ready = [e for e in events if e.source_count >= 2 or (e.first_published_at and e.first_published_at <= wait)]
        return ready[:self.config.max_events]

    def facts(self) -> dict:
        events = self.ready_events()
        self.progress(f'{len(events)} events ready')
        counts = {'events': len(events), 'verified': 0, 'rejected': 0, 'skipped': 0, 'failed': 0}
        if not events:
            return counts
        self.manager.use(*self.worker)
        classifier = EventClassifier(self.client, self.worker_model)
        extractor = FactExtractor(self.client, self.worker_model)
        verifier = FactVerifier(self.client, self.worker_model)
        for number, event in enumerate(events, 1):
            articles = sorted(event.articles.all(), key=lambda a: (a.source.trust_tier, a.published_at or a.fetched_at))
            prefix = f'{number:>3}/{len(events)} event {event.pk} ({event.source_count} sources): {event.title[:60]}'
            try:
                result = self._event_facts(event, articles, classifier, extractor, verifier)
            except LLMError as exc:
                self._fail(event, exc)
                counts['failed'] += 1
                self.progress(f'{prefix} FAILED {exc}')
                continue
            counts['verified'] += result['verified']
            counts['rejected'] += result['rejected']
            counts['skipped'] += event.status == Event.Status.SKIPPED
            self.progress(f'{prefix} -> {event.category or "?"}{" high-risk" if event.high_risk else ""}, '
                          f'{result["verified"]} facts verified, {result["rejected"]} rejected'
                          f'{"" if event.status == Event.Status.FACTS else f" SKIPPED: {event.error}"}')
        return counts

    def _event_facts(self, event, articles, classifier, extractor, verifier) -> dict:
        kind = classifier.classify(articles)
        event.category, event.high_risk = ('' if kind.category == 'other' else kind.category), kind.high_risk
        if kind.category == 'other':
            self._set(event, Event.Status.SKIPPED, 'category other: not finance news')
            return {'verified': 0, 'rejected': 0}

        checked = check_facts(extractor, verifier, event.title, articles)
        with transaction.atomic():
            event.facts.all().delete()
            Fact.objects.bulk_create([
                Fact(event=event, article=c.article or articles[0], statement=c.fact.statement, quote=c.fact.quote,
                     kind=c.fact.kind, status=Fact.Status.VERIFIED if c.verified else Fact.Status.REJECTED,
                     problems=c.problems)
                for c in checked if c.article
            ])
        verified = sum(c.verified for c in checked)
        if verified < self.config.min_facts:
            self._set(event, Event.Status.SKIPPED, f'{verified} verified facts, {self.config.min_facts} needed')
        else:
            self._set(event, Event.Status.FACTS)
        return {'verified': verified, 'rejected': len(checked) - verified}

    # --- writer: the digest ----------------------------------------------------------------------------------

    def digest_events(self) -> list[Event]:
        """Events with verified facts, not in a digest yet, with articles in the period: the most covered first.
        A high-risk event with a single source waits for a second one."""
        since = timezone.now() - timedelta(hours=self.digest_config.period_hours)
        events = (Event.objects.filter(status=Event.Status.FACTS, last_article_at__gte=since)
                  .annotate(source_count=Count('articles__source', distinct=True))
                  .prefetch_related('articles__source')
                  .order_by('-source_count', '-last_article_at'))
        safe = [e for e in events if not e.high_risk or e.source_count >= MIN_INDEPENDENT_SOURCES]
        return safe[:self.digest_config.max_events]

    def digest(self) -> dict:
        events = self.digest_events()
        self.progress(f'{len(events)} events for the digest')
        if not events:
            return {'events': 0}
        briefs = {number: self._brief(number, event) for number, event in enumerate(events, 1)}
        max_items = self.digest_config.max_items
        self.manager.use(*self.writer)
        answer = DigestWriter(self.client, self.writer_model).write(list(briefs.values()), max_items)
        title = digest_title(timezone.localtime(timezone=ZoneInfo(self.digest_config.time_zone)))
        items = kept_items(answer, briefs, max_items)
        with transaction.atomic():
            digest = Digest.objects.create(
                title=title, overview=answer.overview,
                items=[{**item.model_dump(), 'category': briefs[item.events[0]].category} for item in items],
                forecast=[{'asset': asset, **getattr(answer.forecast, asset).model_dump()} for asset, _ in ASSETS],
                messages=render(title, answer, briefs, max_items), writer_model=self.writer_model,
            )
            digest.events.set(events)  # all given, told or left out: a left-out event is not offered again
            for event in events:
                self._set(event, Event.Status.DIGESTED)
        written = {number for item in items for number in item.events}
        for number, event in zip(briefs, events):
            if number not in written:
                self.progress(f'event {event.pk} left out by the writer: {event.title[:60]}')
        self.progress(f'digest {digest.pk}: {title}, {len(items)} items from {len(written)} events, '
                      f'{len(digest.messages)} messages')
        return {'events': len(events), 'items': len(items), 'merged': len(written) - len(items),
                'left_out': len(briefs) - len(written), 'digest': digest.pk}

    @staticmethod
    def _brief(number: int, event: Event) -> EventBrief:
        """The event for the writer; the link goes to its most trusted source."""
        articles = sorted(event.articles.all(), key=lambda a: (a.source.trust_tier, a.published_at or a.fetched_at))
        facts = event.facts.filter(status=Fact.Status.VERIFIED).values_list('statement', flat=True)
        return EventBrief(number=number, category=event.category, sites=event.source_count, facts=list(facts),
                          link_url=articles[0].canonical_url, link_name=articles[0].source.name)

    # --- no model: Telegram ----------------------------------------------------------------------------------

    def send(self) -> dict:
        """Unsent digests to the channel, oldest first. A digest that fails waits for the next run, which posts
        only its messages not posted yet."""
        digests = list(Digest.objects.filter(status=Digest.Status.WRITTEN).order_by('created_at'))
        counts = {'digests': len(digests), 'sent': 0, 'failed': 0}
        token = self.telegram.bot_token.get_secret_value()
        if not digests or not token or not self.telegram.chat_id:
            if digests:
                self.progress('TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is empty: digests wait unsent')
            return counts
        with httpx.Client(timeout=self.telegram.timeout) as http:
            bot = Telegram(http, token, self.telegram.chat_id)
            for digest in digests:
                try:
                    for html in digest.messages[len(digest.message_ids):]:
                        digest.message_ids.append(bot.send(html))
                except TelegramError as exc:
                    digest.error = str(exc)
                    digest.save(update_fields=['message_ids', 'error'])
                    counts['failed'] += 1
                    self.progress(f'digest {digest.pk} FAILED {exc} (sent again next run)')
                    continue
                digest.status, digest.sent_at, digest.error = Digest.Status.SENT, timezone.now(), ''
                digest.save(update_fields=['message_ids', 'status', 'sent_at', 'error'])
                counts['sent'] += 1
                self.progress(f'digest {digest.pk} sent: {digest.title}')
        return counts

    # ---------------------------------------------------------------------------------------------------------

    @staticmethod
    def _set(event: Event, status: str, error: str = '') -> None:
        event.status, event.error = status, error
        event.save(update_fields=['status', 'error', 'category', 'high_risk', 'title', 'updated_at'])

    def _fail(self, event: Event, exc: Exception) -> None:
        self._set(event, Event.Status.FAILED, str(exc))

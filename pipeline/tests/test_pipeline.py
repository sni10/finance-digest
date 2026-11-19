import json
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from pydantic import SecretStr

from llm.client import Completion, Usage
from pipeline.models import Digest, Event, Fact
from pipeline.models_manager import LmsModelManager, LoadSpec
from pipeline.runner import Pipeline
from pipeline.steps.facts import quote_context
from pipeline.telegram import TelegramError
from sources.models import Article, Category, Source

HIKE = ('The Fed raised its policy rate by 25 basis points to 4.75% on Wednesday.\nThe vote was 9 to 3.\n'
        'Gold fell 1.2% after the decision.\nThe Fed did not say when it would move again.')
RUMOUR = ('People familiar with the matter said OPEC+ may cut output by 1 million barrels a day.\nOil rose 3%.\n'
          'No date was given.')


class FakeLM:
    """Embeddings by topic; JSON answers chosen by which step's system prompt is asking."""

    def __init__(self):
        self.calls = []
        self.digest_items = None  # event numbers the writer answers with; None: each event given

    def embed(self, texts, model):
        return [[1.0, 0.0] if 'Fed' in text else [0.0, 1.0] for text in texts]

    def complete_json(self, *, model, system, user, schema, max_tokens, reasoning_effort):
        step = system.split('\n', 1)[0]
        self.calls.append((model, step))
        rumour = 'OPEC' in user
        if step.startswith('You decide whether two'):
            value = {'same_event': True, 'reason': 'same decision'}
        elif step.startswith('You classify one news event'):
            value = {'category': 'energy', 'high_risk': True} if rumour else {'category': 'macro', 'high_risk': False}
        elif step.startswith('You extract'):
            article_id = int(user.split('<article id="', 1)[1].split('"', 1)[0])
            lines = (RUMOUR if rumour else HIKE).split('\n')
            value = {'facts': [{'statement': line.rstrip('.'), 'article_id': article_id, 'quote': line,
                                'kind': 'general'} for line in lines]
                     + [{'statement': 'The Fed raised rates by 50 basis points', 'article_id': article_id,
                         'quote': 'The Fed raised rates by 50 basis points', 'kind': 'number'}]}
        elif step.startswith('You check one statement'):
            statement = user.split('Statement: ', 1)[1].split('\n', 1)[0]
            value = {'verdict': 'supports', 'quote': statement}
        elif step.startswith('You write a short markets digest'):
            given = [int(line.split()[1]) for line in user.split('\n') if line.startswith('Event ')]
            numbers = given if self.digest_items is None else self.digest_items
            value = {'overview': 'ФРС повысила ставку.',
                     'items': [{'event': number, 'headline': 'ФРС повысила ставку до 4,75%',
                                'text': 'Решение приняли 9 голосами против 3.'} for number in numbers],
                     'forecast': {'gold': {'direction': 'down', 'reason': 'Ставка выше, золото дешевле.'},
                                  'brent': {'direction': 'flat', 'reason': 'Новостей по нефти нет.'},
                                  'wti': {'direction': 'flat', 'reason': 'Новостей по нефти нет.'}}}
        else:
            raise AssertionError(f'unexpected prompt: {step}')
        return Completion(schema.model_validate(value), json.dumps(value), model, Usage(10, 5, 0, 0.1))


class FakeManager:
    def __init__(self):
        self.used = []

    def use(self, *specs):
        self.used.append(tuple(spec.model for spec in specs))


class FakeBot:
    """Stands in for the Telegram class: called with (http, token, chat_id), then send() per message."""

    def __init__(self, fail_on=None):
        self.sent, self.fail_on = [], fail_on

    def __call__(self, http, token, chat_id):
        self.token, self.chat_id = token, chat_id
        return self

    def send(self, html):
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            self.fail_on = None
            raise TelegramError('429: Too Many Requests')
        self.sent.append(html)
        return 100 + len(self.sent)


def make_article(source, slug, title, text, hours_ago):
    return Article.objects.create(source=source, canonical_url=f'https://example.com/{slug}', title=title, text=text,
                                  content_hash=slug, status=Article.Status.EXTRACTED,
                                  published_at=timezone.now() - timedelta(hours=hours_ago))


class PipelineTests(TestCase):
    def setUp(self):
        fed = Source.objects.create(name='Federal Reserve', slug='fed', feed_url='https://example.com/fed',
                                    category=Category.MACRO, trust_tier=1)
        cnbc = Source.objects.create(name='CNBC', slug='cnbc', feed_url='https://example.com/cnbc',
                                     category=Category.MARKETS)
        oil = Source.objects.create(name='OilPrice', slug='oil', feed_url='https://example.com/oil',
                                    category=Category.ENERGY)
        make_article(cnbc, 'x1', 'Fed hikes rates again', HIKE, hours_ago=1)
        make_article(fed, 'x2', 'Fed raises the policy rate', HIKE, hours_ago=2)
        make_article(oil, 'h1', 'OPEC+ may cut output, sources say', RUMOUR, hours_ago=10)
        self.lm, self.manager = FakeLM(), FakeManager()

    def run_pipeline(self, events=None, stages=None, token='', **config):
        lm_studio = settings.LM_STUDIO.model_copy(update={'worker_model': 'worker', 'writer_model': 'writer',
                                                          'embedding_model': 'embed'})
        config = settings.PIPELINE.model_copy(update=config)
        # never the real channel: no token unless a test gives one, and then Telegram is faked
        telegram = settings.TELEGRAM.model_copy(update={'bot_token': SecretStr(token), 'chat_id': '@test'})
        pipeline = Pipeline(self.lm, lm_studio, config, self.manager, progress=lambda line: None, telegram=telegram,
                            events=events)
        return pipeline.run(stages) if stages else pipeline.run()

    def test_articles_become_a_digest(self):
        run = self.run_pipeline()

        self.assertEqual(run.stats['group']['new_events'], 2)
        hike, rumour = Event.objects.get(category='macro'), Event.objects.get(category='energy')
        self.assertEqual(hike.articles.count(), 2)

        # the invented "50 basis points" fact fails the code check and is never verified
        self.assertEqual(hike.facts.filter(status=Fact.Status.REJECTED).count(), 1)
        self.assertFalse(hike.facts.filter(status=Fact.Status.VERIFIED, statement__contains='50 basis').exists())

        digest = Digest.objects.get()
        self.assertEqual(list(digest.events.all()), [hike])
        self.assertEqual(hike.status, Event.Status.DIGESTED)
        self.assertEqual([item['category'] for item in digest.items], ['macro'])
        message = digest.messages[0]
        self.assertTrue(message.startswith('<b>Сводка рынков · '))
        self.assertIn('<b>Макро и ставки</b>', message)
        self.assertIn('<a href="https://example.com/x2">Federal Reserve</a> +1', message)  # the most trusted source
        self.assertIn('🔴 ↓ <b>Золото</b>', message)
        self.assertEqual([move['asset'] for move in digest.forecast], ['gold', 'brent', 'wti'])

        # a rumour with one source waits for a second one
        rumour.refresh_from_db()
        self.assertEqual(rumour.status, Event.Status.FACTS)
        # no token: written, not sent
        self.assertEqual(digest.status, Digest.Status.WRITTEN)
        self.assertEqual(run.stats['send'], {'digests': 1, 'sent': 0, 'failed': 0})

    def test_models_are_loaded_by_phase_and_only_the_digest_uses_the_writer(self):
        self.run_pipeline()

        worker = ('worker', 'embed')
        self.assertEqual(self.manager.used, [worker, worker, ('writer',)])
        self.assertEqual({model for model, step in self.lm.calls if step.startswith('You write')}, {'writer'})
        self.assertNotIn('writer', {model for model, step in self.lm.calls if not step.startswith('You write')})

    def test_a_second_run_has_nothing_to_do(self):
        self.run_pipeline()
        calls = len(self.lm.calls)

        run = self.run_pipeline()

        self.assertEqual(len(self.lm.calls), calls)
        self.assertEqual((run.stats['group'], run.stats['digest']), ({'articles': 0}, {'events': 0}))
        self.assertEqual(Digest.objects.count(), 1)

    def test_old_events_stay_out_of_the_digest(self):
        self.run_pipeline(stages=['group', 'facts'])
        Event.objects.update(last_article_at=timezone.now() - timedelta(hours=30))

        run = self.run_pipeline(stages=['digest'])

        self.assertEqual((run.stats['digest'], Digest.objects.count()), ({'events': 0}, 0))

    def test_an_item_for_an_event_not_given_is_dropped(self):
        self.lm.digest_items = [1, 7]

        self.run_pipeline()

        self.assertEqual([item['event'] for item in Digest.objects.get().items], [1])

    def test_a_single_source_event_waits_for_other_sources(self):
        self.run_pipeline(single_source_wait_hours=24)

        rumour = Event.objects.get(articles__canonical_url__endswith='/h1')
        self.assertEqual(rumour.status, Event.Status.COLLECTING)

    def test_the_digest_is_sent_and_a_failed_send_resumes_where_it_stopped(self):
        self.run_pipeline(stages=['group', 'facts', 'digest'])
        digest = Digest.objects.get()
        digest.messages = ['first', 'second']
        digest.save()
        bot = FakeBot(fail_on=1)

        with mock.patch('pipeline.runner.Telegram', bot):
            failed = self.run_pipeline(stages=['send'], token='secret')
            digest.refresh_from_db()
            self.assertEqual((digest.status, digest.message_ids), (Digest.Status.WRITTEN, [101]))
            self.assertIn('429', digest.error)

            sent = self.run_pipeline(stages=['send'], token='secret')

        digest.refresh_from_db()
        self.assertEqual((failed.stats['send']['failed'], sent.stats['send']['sent']), (1, 1))
        self.assertEqual(bot.sent, ['first', 'second'])  # the first message is not posted twice
        self.assertEqual((digest.status, digest.message_ids, digest.error), (Digest.Status.SENT, [101, 102], ''))
        self.assertEqual((bot.token, bot.chat_id), ('secret', '@test'))

    def test_chosen_events_get_facts_again(self):
        self.run_pipeline(stages=['group', 'facts'])
        hike = Event.objects.get(category='macro')
        calls = len(self.lm.calls)

        run = self.run_pipeline(events=[hike.pk], stages=['group', 'facts'])

        self.assertEqual(run.stats['facts']['events'], 1)
        self.assertEqual(run.stats['group'], {'articles': 0, 'skipped': 'chosen events only'})
        self.assertGreater(len(self.lm.calls), calls)

    def test_a_late_article_joins_the_digested_event(self):
        self.run_pipeline()
        make_article(Source.objects.get(slug='oil'), 'x3', 'What the Fed hike means', HIKE, hours_ago=0)

        self.run_pipeline()

        hike = Event.objects.get(category='macro')
        self.assertEqual((hike.articles.count(), hike.has_new_articles), (3, True))


class QuoteContextTests(TestCase):
    def test_the_quote_paragraph_and_its_neighbours(self):
        source = Source.objects.create(name='S', slug='s', feed_url='https://example.com/s', category=Category.MARKETS)
        article = make_article(source, 'q', 'Title', 'One.\nTwo has the quote.\nThree.\nFour.', hours_ago=1)

        self.assertEqual(quote_context('the quote', article), 'One.\nTwo has the quote.\nThree.')


class LmsModelManagerTests(SimpleTestCase):
    def test_unloads_what_is_not_wanted_and_loads_what_is_missing(self):
        commands = []

        def run(args):
            commands.append(args)
            return json.dumps([{'identifier': 'big-writer'}, {'identifier': 'embed'}]) if args[0] == 'ps' else ''

        LmsModelManager(ttl=300, run=run).use(LoadSpec('worker', context=8192), LoadSpec('embed'))

        self.assertEqual(commands, [
            ['ps', '--json'],
            ['unload', 'big-writer'],
            ['load', 'worker', '--parallel', '1', '--ttl', '300', '-y', '--context-length', '8192'],
        ])

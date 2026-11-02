from io import StringIO

import httpx
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from config.app_settings.schema import PublishingSettings
from pipeline.models import Event
from publisher import reactions
from sources.models import Article, Category, Source


def event(title, category, *sources):
    made = Event.objects.create(title=title, category=category)
    for number, source in enumerate(sources):
        made.articles.add(Article.objects.create(source=source, canonical_url=f'https://e.com/{title}/{number}',
                                                 title=title, content_hash=f'{title}{number}',
                                                 published_at=timezone.now()))
    return made


class ReactionTests(TestCase):
    def setUp(self):
        self.wired = Source.objects.create(name='Wired', slug='wired', feed_url='https://e.com/w', category=Category.IT)
        self.coindesk = Source.objects.create(name='CoinDesk', slug='coindesk', feed_url='https://e.com/c',
                                              category=Category.CRYPTO)
        self.liked = event('liked', 'it', self.wired, self.wired)  # two articles of one source: counted once
        self.plain = event('plain', 'crypto', self.coindesk)
        self.never_sent = event('never-sent', 'it', self.wired)
        self.requests = []

    def site(self, request):
        self.requests.append(request)
        return httpx.Response(200, json=[
            {'external_id': f'event-{self.liked.pk}', 'slug': 'liked', 'points': 180, 'readers': 3, 'views': 40},
            {'external_id': f'event-{self.plain.pk}', 'slug': 'plain', 'points': 0, 'readers': 0, 'views': 12},
            {'external_id': 'event-999999', 'slug': 'deleted-here', 'points': 5, 'readers': 1, 'views': 1},
            {'external_id': 'hand-made', 'slug': 'by-an-editor', 'points': 9, 'readers': 1, 'views': 1},
        ])

    def fetch(self, site, key='key'):
        config = PublishingSettings(url='http://site', api_key=key)
        return reactions.fetch(config, http=httpx.Client(transport=httpx.MockTransport(site)))

    def test_reactions_land_on_their_events(self):
        stats = self.fetch(self.site)

        self.assertEqual(stats, {'stories': 4, 'liked': 3, 'events': 2})
        self.assertEqual((self.requests[0].url.path, self.requests[0].url.params['days']),
                         ('/publishing/reactions', '30'))
        self.assertEqual(self.requests[0].headers['x-api-key'], 'key')
        rows = {e.title: (e.site_points, e.site_readers, e.site_views, e.reactions_at is not None)
                for e in Event.objects.all()}
        self.assertEqual(rows, {'liked': (180, 3, 40, True), 'plain': (0, 0, 12, True),
                                'never-sent': (0, 0, 0, False)})

    def test_site_down_or_no_key_changes_nothing(self):
        self.assertIn('failed', self.fetch(lambda request: httpx.Response(503)))
        self.assertEqual(self.fetch(self.site, key=''), {'events': 0})
        self.assertFalse(Event.objects.filter(reactions_at__isnull=False).exists())

    def test_report_by_category_and_source(self):
        self.fetch(self.site)
        out = StringIO()

        call_command('reactions_report', min_stories=1, stdout=out)

        lines = [line.split() for line in out.getvalue().splitlines()]
        self.assertIn(['it', '1', '180.0', '40.0'], lines)
        self.assertIn(['Wired', '1', '180.0', '40.0'], lines)  # its two articles: one story
        self.assertIn(['CoinDesk', '1', '0.0', '12.0'], lines)
        self.assertNotIn('never-sent', out.getvalue())

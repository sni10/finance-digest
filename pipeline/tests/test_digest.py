import json
from datetime import datetime

import httpx
from django.test import SimpleTestCase

from pipeline.steps.digest import (DigestAnswer, DigestItem, EventBrief, Forecast, Move, clean, digest_prompt,
                                   digest_title, kept_items, render, split)
from pipeline.telegram import Telegram, TelegramError


def brief(number, category='macro', sites=1, url='https://example.com/a', name='Fed'):
    return EventBrief(number=number, category=category, sites=sites, facts=['A fact'], link_url=url, link_name=name)


class DigestLayoutTests(SimpleTestCase):
    def test_title_says_morning_or_evening_in_russian(self):
        self.assertEqual(digest_title(datetime(2026, 11, 20, 8, 0)), 'Сводка рынков · 20 ноября, утро')
        self.assertEqual(digest_title(datetime(2026, 11, 20, 20, 0)), 'Сводка рынков · 20 ноября, вечер')

    def test_prompt_numbers_the_events_with_their_facts(self):
        prompt = digest_prompt([brief(1, sites=3), brief(2, category='energy')])

        self.assertIn('Event 1 (macro, 3 sites):\n- A fact', prompt)
        self.assertIn('Event 2 (energy, 1 sites)', prompt)

    def test_items_go_under_their_category_in_a_fixed_order_and_text_is_escaped(self):
        answer = DigestAnswer(overview='Day <1>', items=[
            DigestItem(events=[2], headline='Brent +3%', text='Oil & gas up.'),
            DigestItem(events=[1], headline='Fed hikes', text='By 25 bp.'),
        ], forecast=Forecast(gold=Move(direction='mixed', reason='Both ways.'),
                             brent=Move(direction='up', reason='Supply cut.'),
                             wti=Move(direction='flat', reason='US stocks steady.')))
        events = {1: brief(1, sites=3, url='https://fed.gov/a?x=1&y=2'), 2: brief(2, category='energy', name='EIA')}

        [message] = render('Title', answer, events)

        self.assertLess(message.index('Макро и ставки'), message.index('Нефть и газ'))
        self.assertIn('Day &lt;1&gt;', message)
        self.assertIn('• <b>Fed hikes</b>. By 25 bp. (<a href="https://fed.gov/a?x=1&amp;y=2">Fed</a> +2)', message)
        self.assertIn('Oil &amp; gas up. (<a href="https://example.com/a">EIA</a>)', message)
        forecast = message[message.index('<b>Прогноз</b>'):].split('\n')[1:4]
        self.assertEqual(forecast, ['🟡 ↕ <b>Золото</b> — Both ways.', '🟢 ↑ <b>Нефть Brent</b> — Supply cut.',
                                    '⚪ → <b>Нефть WTI</b> — US stocks steady.'])
        self.assertIn('не инвестиционная рекомендация', message)

    def test_events_of_one_story_make_one_item_linked_to_the_most_covered(self):
        forecast = Forecast(gold=Move(direction='flat', reason='-'), brent=Move(direction='flat', reason='-'),
                            wti=Move(direction='flat', reason='-'))
        answer = DigestAnswer(overview='x', items=[
            DigestItem(events=[2, 1, 9], headline='IPO', text='Record.'),
            DigestItem(events=[1], headline='Again', text='Told already.'),  # its only event is used: dropped
            DigestItem(events=[3], headline='Gold', text='Down.'),
            DigestItem(events=[4], headline='Fourth', text='Past the limit.'),
        ], forecast=forecast)
        events = {1: brief(1, category='markets', sites=7, url='https://wsj.com/ipo', name='WSJ'),
                  2: brief(2, category='markets', sites=4), 3: brief(3, category='metals'), 4: brief(4)}

        self.assertEqual([item.events for item in kept_items(answer, events, max_items=2)], [[2, 1], [3]])
        [message] = render('T', answer, events, max_items=2)
        self.assertIn('• <b>IPO</b>. Record. (<a href="https://wsj.com/ipo">WSJ</a> +6)', message)
        self.assertNotIn('Told already', message)
        self.assertNotIn('Fourth', message)

    def test_event_numbers_never_reach_the_reader(self):
        self.assertEqual(clean('Жесткая риторика ФРС (событие 4) давит на золото, а запасы (события 5 и 12) — на WTI.'),
                         'Жесткая риторика ФРС давит на золото, а запасы — на WTI.')
        self.assertEqual(clean('Как показало событие 10, риски растут.'), 'Как показало, риски растут.')
        self.assertEqual(clean('Ставка 4,5% (до 2027 года).'), 'Ставка 4,5% (до 2027 года).')

    def test_split_keeps_blocks_whole_under_the_limit(self):
        self.assertEqual(split(['a' * 6, 'b' * 6, 'c' * 3], limit=10), ['a' * 6, 'b' * 6, 'c' * 3])
        self.assertEqual(split(['a' * 3, 'b' * 3], limit=10), ['aaa\n\nbbb'])

    def test_a_block_over_the_limit_is_cut_between_items(self):
        self.assertEqual(split(['x' * 5 + '\n\n' + 'y' * 5], limit=8), ['xxxxx', 'yyyyy'])


class TelegramTests(SimpleTestCase):
    def bot(self, handler):
        return Telegram(httpx.Client(transport=httpx.MockTransport(handler)), 'TOKEN123', '@channel')

    def test_posts_html_without_link_previews(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 42}})

        self.assertEqual(self.bot(handler).send('<b>Hi</b>'), 42)
        body = json.loads(requests[0].content)
        self.assertEqual(requests[0].url.path, '/botTOKEN123/sendMessage')
        self.assertEqual((body['chat_id'], body['parse_mode'], body['link_preview_options']),
                         ('@channel', 'HTML', {'is_disabled': True}))

    def test_an_api_error_is_raised_with_its_description(self):
        bot = self.bot(lambda request: httpx.Response(400, json={'ok': False, 'description': "can't parse entities"}))

        with self.assertRaisesRegex(TelegramError, "400: can't parse entities"):
            bot.send('<b>')

    def test_a_network_error_never_shows_the_token(self):
        def handler(request):
            raise httpx.ConnectError(f'cannot reach {request.url}')

        with self.assertRaises(TelegramError) as caught:
            self.bot(handler).send('hi')
        self.assertNotIn('TOKEN123', str(caught.exception))

"""How readers took the stories on the site -> the events (pipeline stage `reactions`)."""
from collections.abc import Callable

import httpx
from django.utils import timezone

from pipeline.models import Event

from .sender import PublishingClient, SendError

DAYS = 30  # older stories hardly get new likes or views


def save_reactions(rows: list[dict], now=None) -> int:
    """Events updated. Stories that are not ours (no event-<pk>, or a deleted event) are left out."""
    now = now or timezone.now()
    by_pk = {int(row['external_id'].removeprefix('event-')): row for row in rows
             if row['external_id'].removeprefix('event-').isdigit()}
    events = list(Event.objects.filter(pk__in=by_pk))
    for event in events:
        row = by_pk[event.pk]
        event.site_points, event.site_readers, event.site_views = row['points'], row['readers'], row['views']
        event.reactions_at = now
    Event.objects.bulk_update(events, ['site_points', 'site_readers', 'site_views', 'reactions_at'])
    return len(events)


def fetch(config, progress: Callable[[str], None] = lambda line: None, http: httpx.Client | None = None) -> dict:
    api_key = config.api_key.get_secret_value()
    if not api_key:
        progress('PUBLISHING_API_KEY is empty: no reactions fetched')
        return {'events': 0}
    with http or httpx.Client(timeout=config.timeout) as client:
        try:
            rows = PublishingClient(client, config.url, api_key).reactions(DAYS)
        except SendError as exc:  # the site is down: the next run tries again, nothing is lost
            progress(f'reactions FAILED — {exc}')
            return {'events': 0, 'failed': str(exc)}
    updated = save_reactions(rows)
    liked = sum(1 for row in rows if row['points'])
    progress(f'{len(rows)} stories of the last {DAYS} days on the site, {liked} liked; {updated} events updated')
    return {'stories': len(rows), 'liked': liked, 'events': updated}

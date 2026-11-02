"""What lands with readers: like points and views per story, by source and by category (stage `reactions` data)."""
from collections import defaultdict

from django.core.management.base import BaseCommand

from pipeline.models import Event


def table(events, key) -> list[tuple[str, int, float, float]]:
    """(name, stories, points per story, views per story), most points per story first. A story counts once per
    source even when the source wrote several of its articles."""
    groups = defaultdict(list)
    for event in events:
        for name in key(event):
            groups[name].append(event)
    rows = [(name, len(group), sum(e.site_points for e in group) / len(group),
             sum(e.site_views for e in group) / len(group)) for name, group in groups.items()]
    return sorted(rows, key=lambda row: (-row[2], -row[3], row[0]))


class Command(BaseCommand):
    help = 'Like points and views per story on the site, by source and by category. Run pipeline --stage reactions first.'

    def add_arguments(self, parser):
        parser.add_argument('--min-stories', type=int, default=3, help='Sources with fewer stories are left out')

    def handle(self, min_stories, **options):
        events = list(Event.objects.filter(reactions_at__isnull=False).prefetch_related('articles__source'))
        if not events:
            self.stdout.write('No reactions yet: run pipeline --stage reactions')
            return
        self.stdout.write(f'{len(events)} stories on the site\n')
        for title, key, minimum in (('category', lambda e: [e.category or '?'], 1),
                                    ('source', lambda e: {a.source.name for a in e.articles.all()}, min_stories)):
            self.stdout.write(self.style.MIGRATE_HEADING(f'{title:<30} stories  points/story  views/story'))
            for name, stories, points, views in table(events, key):
                if stories >= minimum:
                    self.stdout.write(f'{name[:30]:<30} {stories:>7}  {points:>11.1f}  {views:>11.1f}')
            self.stdout.write('')

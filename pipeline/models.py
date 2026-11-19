"""Pipeline state (local DB): events grouped from articles, their facts, and the digests written from them."""
from django.db import models

from sources.models import Article, Category


class ArticleEmbedding(models.Model):
    """The article's embedding for grouping, computed once (title + start of the text)."""

    article = models.OneToOneField(Article, on_delete=models.CASCADE, primary_key=True, related_name='embedding')
    model = models.CharField(max_length=200)
    vector = models.JSONField()  # normalized floats; a few hundred articles a day: numpy is enough, no index


class Event(models.Model):
    """One real-world happening, reported by one or more articles."""

    class Status(models.TextChoices):
        COLLECTING = 'collecting', 'Waiting for more sources'
        FACTS = 'facts', 'Facts verified'
        DIGESTED = 'digested', 'In a digest'
        SKIPPED = 'skipped', 'Not for the digest (category other, too few facts)'
        FAILED = 'failed', 'A step failed: see error'

    title = models.CharField(max_length=500, help_text='Title of the first article of the event')
    category = models.CharField(max_length=10, choices=Category, blank=True)
    high_risk = models.BooleanField(
        default=False, help_text='Rumours and unnamed sources, fraud, charges and fines: two sources needed',
    )
    articles = models.ManyToManyField(Article, related_name='events')
    status = models.CharField(max_length=20, choices=Status, default=Status.COLLECTING, db_index=True)
    first_published_at = models.DateTimeField(null=True, blank=True)
    last_article_at = models.DateTimeField(null=True, blank=True, db_index=True)
    has_new_articles = models.BooleanField(default=False, help_text='Articles added after it went into a digest')
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_article_at', '-id']

    def __str__(self):
        return self.title

    @property
    def sources(self) -> set[str]:
        return {article.source.slug for article in self.articles.all()}


class Fact(models.Model):
    """A statement extracted from one article, with the quote it rests on."""

    class Status(models.TextChoices):
        VERIFIED = 'verified', 'Verified'
        REJECTED = 'rejected', 'Rejected'

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='facts')
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='facts')
    statement = models.TextField()
    quote = models.TextField()
    kind = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=Status)
    problems = models.JSONField(default=list, blank=True, help_text='Why it was rejected')

    class Meta:
        ordering = ['event', 'id']

    def __str__(self):
        return self.statement


class Digest(models.Model):
    """One morning or evening digest: the period's most covered events in short, and a rough forecast."""

    class Status(models.TextChoices):
        WRITTEN = 'written', 'Written, not sent yet'
        SENT = 'sent', 'Sent to Telegram'

    events = models.ManyToManyField(Event, related_name='digests')
    title = models.CharField(max_length=200)
    overview = models.TextField(help_text='The main theme of the period, 1-3 sentences')
    items = models.JSONField(default=list, help_text='[{event, category, headline, text}] as the writer gave them')
    forecast = models.JSONField(default=list, help_text='[{asset, direction, reason}] for gold, brent, wti')
    messages = models.JSONField(default=list, help_text='The Telegram HTML messages, as sent')
    status = models.CharField(max_length=20, choices=Status, default=Status.WRITTEN, db_index=True)
    writer_model = models.CharField(max_length=200)
    message_ids = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, help_text='The last failed send')
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class PipelineRun(models.Model):
    """One `pipeline` command: what each stage did."""

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    stats = models.JSONField(default=dict)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at']

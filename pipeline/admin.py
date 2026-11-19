from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .models import Digest, Event, Fact, PipelineRun

class FactInline(admin.TabularInline):
    model = Fact
    fields = ['status', 'statement', 'quote', 'article', 'problems']
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ['title', 'category', 'high_risk', 'status', 'source_count', 'last_article_at']
    list_filter = ['status', 'category', 'high_risk']
    search_fields = ['title']
    readonly_fields = ['articles_list', 'first_published_at', 'last_article_at', 'has_new_articles', 'error',
                       'created_at', 'updated_at']
    fields = ['title', 'category', 'high_risk', 'status', *readonly_fields]
    inlines = [FactInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('articles__source')

    @admin.display(description='sources')
    def source_count(self, event):
        return len(event.sources)

    @admin.display(description='articles')
    def articles_list(self, event):
        rows = ((reverse('admin:sources_article_change', args=[a.pk]), a.source.name, a.title, a.canonical_url)
                for a in event.articles.all())
        return format_html('<ul>{}</ul>', format_html_join(
            '', '<li><a href="{}">{}: {}</a> (<a href="{}" target="_blank">original</a>)</li>', rows))


@admin.register(Digest)
class DigestAdmin(admin.ModelAdmin):
    """The digests as they went to Telegram. Status back to "written" sends a digest again with the next run."""

    list_display = ['title', 'status', 'event_count', 'created_at', 'sent_at', 'error']
    list_filter = ['status']
    readonly_fields = ['preview', 'events_list', 'forecast', 'writer_model', 'message_ids', 'error', 'created_at',
                       'sent_at']
    fields = ['title', 'status', *readonly_fields]

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('events')

    @admin.display(description='events')
    def event_count(self, digest):
        return len(digest.events.all())

    @admin.display(description='messages (Telegram HTML)')
    def preview(self, digest):
        return format_html_join('', '<pre style="white-space: pre-wrap; max-width: 46em">{}</pre>',
                                ((message,) for message in digest.messages))

    @admin.display(description='events')
    def events_list(self, digest):
        rows = ((reverse('admin:pipeline_event_change', args=[e.pk]), e.title) for e in digest.events.all())
        return format_html('<ul>{}</ul>', format_html_join('', '<li><a href="{}">{}</a></li>', rows))


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    list_display = ['started_at', 'finished_at', 'stats', 'error']
    readonly_fields = ['started_at', 'finished_at', 'stats', 'error']

import json

from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html

from sources.admin import ArticleAdmin
from sources.models import Article

from .models import EvalEvent, EvalFact, EvalResult, EvalRun


class EvalFactInline(admin.TabularInline):
    model = EvalFact
    extra = 3


@admin.register(EvalEvent)
class EvalEventAdmin(admin.ModelAdmin):
    list_display = ['title', 'category', 'article_count', 'fact_count', 'high_risk', 'created_at']
    list_filter = ['category', 'high_risk']
    search_fields = ['title', 'notes']
    autocomplete_fields = ['articles']
    inlines = [EvalFactInline]

    @admin.display(description='articles')
    def article_count(self, event):
        return event.articles.count()

    @admin.display(description='facts')
    def fact_count(self, event):
        return event.facts.count()


@admin.register(EvalRun)
class EvalRunAdmin(admin.ModelAdmin):
    list_display = ['task', 'model_id', 'started_at', 'finished_at', 'metrics', 'results_link']
    list_filter = ['task', 'model_id']
    readonly_fields = ['task', 'model_id', 'params', 'metrics', 'started_at', 'finished_at']

    @admin.display(description='results')
    def results_link(self, run):
        url = reverse('admin:evaluation_evalresult_changelist') + f'?run__id__exact={run.pk}'
        return format_html('<a href="{}">{} answers</a>', url, run.results.count())


@admin.register(EvalResult)
class EvalResultAdmin(admin.ModelAdmin):
    """Answers of the runs."""

    list_display = ['id', 'run', 'event', 'correct', 'manual_score', 'seconds', 'short_output', 'error']
    list_filter = ['run__task', 'run__model_id', 'correct', 'run']
    list_editable = ['manual_score']
    list_select_related = ['run', 'event']
    readonly_fields = ['run', 'event', 'article', 'correct', 'error', 'prompt_tokens', 'completion_tokens',
                       'reasoning_tokens', 'seconds', 'pretty_output', 'raw']
    exclude = ['output']

    @admin.display(description='output')
    def short_output(self, result):
        answer = (result.output or {}).get('answer')
        return str(answer)[:150] if answer is not None else ''

    @admin.display(description='output')
    def pretty_output(self, result):
        return format_html('<pre style="white-space: pre-wrap">{}</pre>',
                           json.dumps(result.output, indent=2, ensure_ascii=False))

    def has_add_permission(self, request):
        return False


# Building the test set: tick the articles of one event in the article list -> action -> fill in the event form
admin.site.unregister(Article)


@admin.register(Article)
class ArticleWithEvalAdmin(ArticleAdmin):
    actions = ['create_eval_event']

    @admin.action(description='Create eval event from selected articles')
    def create_eval_event(self, request, queryset):
        articles = list(queryset.select_related('source'))
        event = EvalEvent.objects.create(title=articles[0].title[:300], category=articles[0].source.category)
        event.articles.set(articles)
        self.message_user(request, f'Event created from {len(articles)} articles: add its key facts', messages.SUCCESS)
        return redirect(reverse('admin:evaluation_evalevent_change', args=[event.pk]))

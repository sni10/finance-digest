import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from llm.client import LLMError, LMStudioClient
from pipeline.models_manager import ApiModelManager, LmsModelManager, ModelLoadError, NoModelManager
from pipeline.runner import STAGES, Pipeline


class Command(BaseCommand):
    help = ('New articles -> events -> facts -> digest -> Telegram (admin: Pipeline -> Digests). '
            'Collect first (collect). Stops halfway safely: the next run continues.')

    def add_arguments(self, parser):
        parser.add_argument('--stage', action='append', dest='stages', choices=STAGES,
                            help='Only these stages (repeatable). Default: all, in order')
        parser.add_argument('--max-events', type=int, help='Events taken to facts in this run (PIPELINE_MAX_EVENTS)')
        parser.add_argument('--event', type=int, action='append', dest='events', metavar='ID',
                            help='Extract the facts of these events again (repeatable), whatever their status')
        parser.add_argument('--no-manage-models', action='store_true',
                            help='Do not load/unload models with lms: LM Studio loads them on demand')

    def handle(self, stages, max_events, no_manage_models, events, **options):
        config = settings.PIPELINE
        if max_events:
            config = config.model_copy(update={'max_events': max_events})
        client = LMStudioClient.from_settings(settings.LM_STUDIO)
        stages = stages or list(STAGES)
        if not set(stages) <= {'send'}:  # sending needs no LM Studio
            try:
                client.list_models()
            except LLMError as exc:
                # the PC is off or unreachable: what is written still goes out, the rest waits for the next run
                if 'send' in stages:
                    self.stdout.write(self.style.WARNING('LM Studio unreachable: sending what is written only'))
                    Pipeline(client, settings.LM_STUDIO, config, NoModelManager(),
                             progress=self.stdout.write).run(['send'])
                raise CommandError(f'LM Studio unreachable, nothing new this run: {exc}') from exc

        with httpx.Client(timeout=settings.LM_STUDIO.timeout) as http:
            manager = self._manager(config, no_manage_models, http)
            pipeline = Pipeline(client, settings.LM_STUDIO, config, manager, progress=self.stdout.write,
                                events=events)
            try:
                run = pipeline.run(stages)
            except ModelLoadError as exc:
                raise CommandError(str(exc)) from exc
            finally:
                try:
                    manager.release()  # the writer is not left in the PC's VRAM after a server run
                except ModelLoadError as exc:
                    self._note(f'release failed: {exc}')
        for stage, stats in run.stats.items():
            shown = {key: value for key, value in stats.items() if key != 'rejected'}
            self.stdout.write(self.style.MIGRATE_HEADING(stage) + f' {shown}')
        self.stdout.write(self.style.SUCCESS(f'run {run.pk} done: digests in /admin/pipeline/digest/'))

    def _manager(self, config, no_manage_models, http):
        if not config.manage_models or no_manage_models:
            return NoModelManager()
        if config.model_loader == 'api':
            lm_studio = settings.LM_STUDIO
            root = lm_studio.base_url.rstrip('/').removesuffix('/v1')
            return ApiModelManager(http, root, lm_studio.api_key.get_secret_value(), progress=self._note)
        return LmsModelManager(config.idle_ttl, progress=self._note)

    def _note(self, line: str) -> None:
        self.stdout.write(self.style.WARNING(f'[lms] {line}'))

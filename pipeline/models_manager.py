"""One big model in VRAM at a time (spec §4): the worker (with the embedding model) by day, the writer for the
cards. Through LM Studio's `lms` CLI, with explicit context and parallelism: LM Studio's JIT loading takes the
model's defaults (4 parallel slots, large context) and keeps a 27B half in RAM next to a 9B."""
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass


class ModelLoadError(Exception):
    pass


@dataclass(frozen=True)
class LoadSpec:
    model: str
    context: int | None = None
    gpu: str | None = None  # offload ratio, None = LM Studio decides

    def args(self, ttl: int) -> list[str]:
        args = ['load', self.model, '--parallel', '1', '--ttl', str(ttl), '-y']
        if self.context:
            args += ['--context-length', str(self.context)]
        if self.gpu:
            args += ['--gpu', self.gpu]
        return args


class LmsModelManager:
    def __init__(self, ttl: int, run: Callable[[list[str]], str] | None = None, progress=lambda line: None):
        self.ttl = ttl
        self.run = run or _run_lms
        self.progress = progress

    def use(self, *specs: LoadSpec) -> None:
        """Exactly these models loaded: others are unloaded first, so VRAM is free for the big one."""
        loaded = self.loaded()
        wanted = {spec.model for spec in specs}
        for model in loaded - wanted:
            self.progress(f'unload {model}')
            self.run(['unload', model])
        for spec in specs:
            if spec.model not in loaded:
                self.progress(f'load {spec.model}')
                self.run(spec.args(self.ttl))

    def loaded(self) -> set[str]:
        items = json.loads(self.run(['ps', '--json']) or '[]')
        return {item.get('identifier') or item.get('modelKey') or item.get('path') for item in items} - {None}


    def release(self) -> None:
        """Nothing to do: the models were loaded with a TTL, LM Studio unloads them when idle."""


class ApiModelManager:
    """The same through LM Studio's REST API (/api/v1/models), for a pipeline on another machine (the server): no
    `lms` there. The API has no TTL, parallel or GPU-ratio options: LM Studio takes the model's own defaults for
    those (set them once in My Models), and release() unloads what this run loaded."""

    def __init__(self, http, root_url: str, api_key: str = '', progress=lambda line: None):
        self.http = http
        self.root = root_url.rstrip('/')
        self.headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
        self.progress = progress
        self.loaded_here: set[str] = set()

    def use(self, *specs: LoadSpec) -> None:
        instances = self.instances()
        wanted = {spec.model for spec in specs}
        for instance, model in instances.items():
            if model not in wanted:
                self._unload(instance)
        present = {model for instance, model in instances.items() if model in wanted}
        for spec in specs:
            if spec.model not in present:
                self.progress(f'load {spec.model}')
                body = {'model': spec.model} | ({'context_length': spec.context} if spec.context else {})
                answer = self._post('/api/v1/models/load', body)
                self.loaded_here.add(answer.get('instance_id') or spec.model)

    def release(self) -> None:
        if not self.loaded_here:
            return
        for instance in set(self.instances()) & self.loaded_here:
            self._unload(instance)

    def instances(self) -> dict[str, str]:
        """Loaded instance id -> model key."""
        models = self._call('GET', '/api/v1/models').get('models', [])
        return {instance['id']: model['key'] for model in models for instance in model.get('loaded_instances', [])}

    def _unload(self, instance: str) -> None:
        self.progress(f'unload {instance}')
        self._post('/api/v1/models/unload', {'instance_id': instance})
        self.loaded_here.discard(instance)

    def _post(self, path: str, body: dict) -> dict:
        return self._call('POST', path, body)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            response = self.http.request(method, self.root + path, json=body, headers=self.headers)
        except Exception as exc:  # httpx errors: LM Studio down, PC off, Tailscale gone
            raise ModelLoadError(f'LM Studio {method} {path}: {exc}') from exc
        if response.status_code >= 400:
            raise ModelLoadError(f'LM Studio {method} {path}: {response.status_code} {response.text[:300]}')
        return response.json()


class NoModelManager:
    """PIPELINE_MANAGE_MODELS=false: LM Studio loads models on demand (JIT) itself."""

    def use(self, *specs: LoadSpec) -> None:
        pass

    def release(self) -> None:
        pass


def _run_lms(args: list[str]) -> str:
    executable = shutil.which('lms')
    if not executable:
        raise ModelLoadError('lms CLI not found: install it from LM Studio or set PIPELINE_MANAGE_MODELS=false')
    result = subprocess.run([executable, *args], capture_output=True, text=True, encoding='utf-8', timeout=600)
    if result.returncode:
        raise ModelLoadError(f'lms {" ".join(args)}: {result.stderr.strip() or result.stdout.strip()}')
    return result.stdout

"""Bridge to the deployment's sole owned-process controller.

The module path is deployment configuration, never accepted from HTTP input.
The controller owns process identity, allowlists and cross-process locking.
"""
from functools import lru_cache
import importlib.util
from pathlib import Path
import math
import re
from threading import local


class RuntimeUnavailable(RuntimeError):
    pass


def configured(config):
    return bool(config.get('LOCAL_MODEL_CONTROLLER') or config.get('LOCAL_MODEL_CONTROLLER_PATH'))


@lru_cache(maxsize=2)
def _load(path):
    location = Path(path)
    if not location.is_absolute() or not location.is_file():
        raise RuntimeUnavailable('本机模型控制器未配置')
    spec = importlib.util.spec_from_file_location('research_local_runtime_controller', location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def controller(config):
    instance = config.get('LOCAL_MODEL_CONTROLLER')
    if instance is not None:
        return instance
    path = config.get('LOCAL_MODEL_CONTROLLER_PATH')
    if not path:
        raise RuntimeUnavailable('本机模型控制器未配置')
    return _load(str(path))


def runtime_status(config):
    return controller(config).model_status()


class RuntimeAssistant:
    """Resolve once per operation under a controller-held inference lease."""
    provider_kind = 'LOCAL'

    def __init__(self, assistant_class, config, *, required_capability=None, **options):
        self.assistant_class = assistant_class
        self.config = config
        self.options = options
        self.required_capability = required_capability
        self._local = local()

    @property
    def last_metadata(self):
        return dict(getattr(self._local, 'metadata', {}))

    @property
    def model_version(self):
        return getattr(self._local, 'model', None) or runtime_status(self.config).get('model')

    @property
    def base_url(self):
        return runtime_status(self.config).get('endpoint')

    def _invoke(self, method, *args, **kwargs):
        self._local.metadata = {}
        self._local.model = None
        with controller(self.config).inference_lease() as active:
            if active.get('state') != 'ready' or active.get('identityVerified') is not True:
                raise RuntimeUnavailable('本地模型尚未就绪')
            if self.required_capability and self.required_capability not in active.get('capabilities', []):
                raise RuntimeUnavailable('当前本地模型不支持所需能力')
            provider = self.assistant_class(base_url=active['endpoint'], model=active['model'], **self.options)
            self._local.model = active['model']
            result = getattr(provider, method)(*args, **kwargs)
            self._local.metadata = getattr(provider, 'last_metadata', {})
            return result

    def generate(self, *args, **kwargs):
        return self._invoke('generate', *args, **kwargs)

    def suggest(self, *args, **kwargs):
        return self._invoke('suggest', *args, **kwargs)


def make_assistant(assistant_class, config, *, required_capability=None, **options):
    if configured(config):
        return RuntimeAssistant(
            assistant_class, config,
            required_capability=required_capability, **options,
        )
    return assistant_class(base_url=config.get('LOCAL_MODEL_BASE_URL'),
                           model=config.get('LOCAL_MODEL_NAME'), **options)


_STATUS_ERRORS = {
    'MODEL_BUSY': '模型正在使用或切换',
    'MODEL_NOT_AVAILABLE': '模型文件尚未准备完成',
    'IDENTITY_MISMATCH': '模型运行身份未通过校验',
    'RUNTIME_UNAVAILABLE': '本地模型状态暂不可用',
    'MODEL_OPERATION_FAILED': '模型操作未完成，请刷新查看实际状态',
}


def public_status(value):
    """Publish only the stable UI contract, never controller internals/errors."""
    from app.ai.local_model import _validated_base_url
    if not isinstance(value, dict):
        raise RuntimeUnavailable('本地模型状态暂不可用')
    profiles = []
    for item in value.get('profiles', []):
        if not isinstance(item, dict):
            continue
        identifier = item.get('id')
        name = item.get('name')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', identifier):
            continue
        profiles.append({'id': identifier, 'name': name if isinstance(name, str) else identifier,
                         'expectedModel': item.get('expectedModel') if isinstance(item.get('expectedModel'), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', item['expectedModel']) else None,
                         'available': item.get('available') is True,
                         'visionCapable': item.get('visionCapable') is True,
                         'unavailableReason': item.get('unavailableReason') if item.get('unavailableReason') in {'NOT_INSTALLED', 'HASH_UNVERIFIED'} else None})
    state = value.get('state')
    if state not in {'unloaded', 'loading', 'ready', 'failed'}:
        state = 'failed'
    profile = value.get('profileId', value.get('loadedProfileId'))
    if profile not in {item['id'] for item in profiles}:
        profile = None
    model = value.get('model')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', model):
        model = None
    try:
        endpoint = _validated_base_url(value.get('endpoint'))
    except (ValueError, TypeError, AttributeError):
        endpoint = None
    identity = value.get('identityVerified') is True
    matched_profile = next((item for item in profiles if item['id'] == profile), {})
    if state == 'ready' and not (identity and model and endpoint and profile
                                 and matched_profile.get('available') is True
                                 and model == matched_profile.get('expectedModel')):
        state = 'failed'
    def measurement(number):
        return number if type(number) in (int, float) and math.isfinite(number) and number >= 0 else None
    memory = value.get('systemMemory')
    memory = memory if isinstance(memory, dict) else {}
    code = value.get('errorCode')
    if code not in _STATUS_ERRORS:
        code = 'RUNTIME_UNAVAILABLE' if state == 'failed' else None
    return {'state': state, 'profiles': profiles, 'profileId': profile,
            'loadedProfileId': profile if state == 'ready' else None,
            'model': model, 'endpoint': endpoint, 'identityVerified': identity,
            'visionVerified': value.get('visionVerified') is True,
            'capabilities': [item for item in value.get('capabilities', [])
                             if item in {'text', 'image'}] if isinstance(value.get('capabilities'), list) else [],
            'processMemoryMiB': measurement(value.get('processMemoryMiB')),
            'processMemorySource': value.get('processMemorySource') if value.get('processMemorySource') in {'ps_rss', 'psutil_rss'} else None,
            'systemMemory': {**{key: measurement(memory.get(key)) for key in ('totalMiB', 'availableMiB', 'usedMiB')},
                             'source': memory.get('source') if memory.get('source') == 'psutil_virtual_memory' else None},
            'errorCode': code, 'error_message': _STATUS_ERRORS.get(code, '')}

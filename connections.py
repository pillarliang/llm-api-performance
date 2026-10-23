"""Request-scoped API connections. Secrets never enter benchmark settings."""
import hashlib
import os
import re
from urllib.parse import urlsplit, urlunsplit


def normalize_base(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请填写 Base URL')
    value = value.strip()
    if '://' not in value:
        value = 'https://' + value
    parts = urlsplit(value)
    if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or
            parts.password or parts.query or parts.fragment or any(c.isspace() for c in value)):
        raise ValueError('Base URL 必须为不含凭证、查询参数的 HTTP(S) 地址')
    try:
        parts.port
    except ValueError:
        raise ValueError('Base URL 端口无效') from None
    path = parts.path.rstrip('/')
    # Preserve arbitrary OpenAI-compatible proxy paths. Only normalize known SG management URLs.
    if parts.hostname.endswith('.aliyuncs.com'):
        if path.endswith('/api/v1'):
            path = path[:-7] + '/compatible-mode/v1'
        elif not path:
            path = '/compatible-mode/v1'
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def suggest_base_url(value):
    """Suggest a base for common pasted endpoint URLs; never rewrite proxy paths."""
    try:
        base = normalize_base(value)
    except ValueError:
        return None
    parts = urlsplit(base)
    match = re.search(r'/(?:chat/completions|completions|models|responses)(/v1)?$', parts.path)
    if not match:
        return None
    path = parts.path[:match.start()]
    if parts.hostname == 'api.deepseek.com' and not path:
        path = ''
    elif match.group(1) and not path.endswith('/v1'):
        path += '/v1'
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def default_connection(fallback):
    base = os.environ.get('DASHSCOPE_BASE_URL') or os.environ.get('MODELSTUDIO_HOST_SG') or fallback
    name = 'DASHSCOPE_API_KEY' if os.environ.get('DASHSCOPE_API_KEY') else 'MODELSTUDIO_API_KEY_SG'
    return normalize_base(base), name, os.environ.get(name, '')


def resolve_connection(value, fallback, require_key=True):
    if not isinstance(value, dict):
        raise ValueError('连接配置必须为对象')
    if value.get('mode', 'default') == 'default':
        base, name, key = default_connection(fallback)
        if value.get('base_url') and normalize_base(value['base_url']) != base:
            raise ValueError('使用其他 Base URL 时，请切换自定义连接并填写自己的 API Key')
        if value.get('api_key'):
            raise ValueError('填写自己的 API Key 时，请切换自定义连接')
    elif value.get('mode') == 'custom':
        base = normalize_base(value.get('base_url'))
        key = value.get('api_key', '')
        if not isinstance(key, str) or len(key) > 8192 or any(c.isspace() for c in key):
            raise ValueError('API Key 格式无效')
        # Account changes must not reuse another account's successful incremental cases.
        name = 'CUSTOM_API_KEY_' + hashlib.sha256(key.encode()).hexdigest()[:20]
    else:
        raise ValueError('未知连接类型')
    if require_key and not key:
        raise ValueError('请填写 API Key' if value.get('mode') == 'custom' else '服务端尚未配置 SG API Key')
    return base, name, key


def normalize_providers(values):
    """Persist source metadata only; credentials travel separately with each request."""
    if not isinstance(values, list) or not values:
        raise ValueError('请至少添加一个连接源')
    result, ids = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError('连接源必须为对象')
        identifier = value.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier) or identifier in ids:
            raise ValueError('连接源 ID 必须唯一，且只包含字母、数字、下划线或短横线')
        name = value.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('请填写连接源名称（最多 100 字）')
        if value.get('mode') not in {'default', 'custom'}:
            raise ValueError('未知连接类型')
        if 'api_key' in value:
            raise ValueError('API Key 必须与连接源配置分开提交')
        result.append(dict(id=identifier, name=name.strip(), mode=value['mode'], base_url=normalize_base(value.get('base_url'))))
        ids.add(identifier)
    return result


def bind_providers(config, supplied, fallback, require_key=False):
    providers = normalize_providers(config['providers'])
    if not isinstance(supplied, list):
        raise ValueError('连接源凭证必须为列表')
    secrets = {}
    known = {p['id'] for p in providers}
    for item in supplied:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] not in known or item['id'] in secrets:
            raise ValueError('连接源凭证包含未知或重复的 ID')
        secrets[item['id']] = item.get('api_key', '')
    models = config.get('models', [])
    if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
        raise ValueError('models 必须为对象列表')
    if any(not isinstance(m.get('provider_id'), str) or m['provider_id'] not in known for m in models):
        raise ValueError('模型引用的连接源不存在')
    used = {m['provider_id'] for m in models if m.get('enabled', True)}
    resolved, credentials = {}, {}
    for provider in providers:
        connection = dict(provider, api_key=secrets.get(provider['id'], ''))
        try:
            base, name, key = resolve_connection(connection, fallback, require_key=require_key and provider['id'] in used)
        except ValueError as exc:
            raise ValueError(f"{provider['name']}：{exc}") from exc
        provider['base_url'] = base
        resolved[provider['id']] = (provider, name)
        if key and provider['id'] in used:
            credentials[name] = key
    for model in models:
        provider, name = resolved[model['provider_id']]
        model.pop('base_url_env', None)
        model.update(base_url=provider['base_url'], api_key_env=name, provider_name=provider['name'])
    config['providers'] = providers
    return credentials

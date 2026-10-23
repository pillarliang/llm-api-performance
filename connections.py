"""Request-scoped API connections. Secrets never enter benchmark settings."""
import hashlib
import os
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

"""FreeLLMAPI configuration, discovery and explicit per-model compatibility checks.

Uses AIRI's private api-keys directory; no credentials in public state or errors.
Inference stays in llm_client, personality/context/tools stay in agent_runtime.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.error
from urllib.parse import urlsplit

class ProviderError(ValueError):
    """Only locally composed, credential-free errors."""

PREFIX = 'freellmapi:'
_lock = threading.RLock()
_reports = {}
_catalogs = {}
_probe_lock = threading.Lock()


def data_dir():
    return Path(os.environ.get('AIRI_DATA', str(Path(__file__).resolve().parent / 'data')))


def validate_model(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+~-]{0,255}', value):
        raise ValueError('FreeLLMAPI 모델 ID를 확인하세요 (최대 256자).')
    return value


def validate_url(value):
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment or not p.port and p.netloc.endswith(':'):
            raise ValueError()
        if any(ord(c) < 33 for c in value) or len(value) > 1000:
            raise ValueError()
        if p.scheme == 'http' and p.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError()
        _ = p.port
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Base URL은 로컬 HTTP 또는 HTTPS 주소여야 합니다. URL에 키·사용자 정보·쿼리를 넣지 마세요.') from None
    return value.rstrip('/')


def config():
    values = {'enabled': os.environ.get('FREELLMAPI_ENABLED', 'false').lower() == 'true',
              'base_url': os.environ.get('FREELLMAPI_BASE_URL', 'http://127.0.0.1:3001/v1'),
              'model': os.environ.get('FREELLMAPI_MODEL', 'auto'), 'timeout': 90}
    path = data_dir() / 'freellmapi.json'
    try:
        values.update(json.loads(path.read_text()))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        raise ValueError('FreeLLMAPI 설정 파일을 읽지 못했습니다.') from None
    values['base_url'] = validate_url(values['base_url'])
    values['model'] = validate_model(values['model'])
    # Saved credentials take precedence; an explicitly cleared file overrides env.
    keyfile = data_dir() / 'api-keys' / 'freellmapi'
    try:
        values['key'] = keyfile.read_text().strip()
    except FileNotFoundError:
        values['key'] = os.environ.get('FREELLMAPI_API_KEY', '').strip()
    return values


def _write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8))
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
            f.write(value)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save(body):
    with _lock:
        current = config()
        values = {k: body.get(k, current[k]) for k in ('enabled', 'base_url', 'model', 'timeout')}
        if type(values['enabled']) is not bool:
            raise ValueError('Enabled 값은 true 또는 false여야 합니다.')
        values['base_url'] = validate_url(values['base_url'])
        values['model'] = validate_model(values['model'])
        if type(values['timeout']) is not int or not 5 <= values['timeout'] <= 180:
            raise ValueError('응답 제한 시간은 5–180초로 지정하세요.')
        key = body.get('api_key', '')
        if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise ValueError('API 키 형식을 확인하세요.')
        if key or body.get('clear_key'):
            _write_private(data_dir() / 'api-keys' / 'freellmapi', '' if body.get('clear_key') else key.strip())
        _write_private(data_dir() / 'freellmapi.json', json.dumps(values))
        _reports.clear()
        _catalogs.clear()
        return public()


def endpoint(selected):
    c = config()
    if not c['enabled']:
        raise ValueError('FreeLLMAPI가 꺼져 있습니다. 설치·연결에서 활성화하세요.')
    return {'provider': 'FreeLLMAPI', 'base_url': c['base_url'], 'key': c['key'],
            'model': validate_model(selected[len(PREFIX):]), 'response_timeout': c['timeout']}


def identity(model=None):
    c = config()
    return hashlib.sha256(json.dumps([c['base_url'], c['key'], model or c['model'], c['timeout'], c['enabled']]).encode()).hexdigest()


def error_reason(error):
    if isinstance(error, ProviderError):
        return str(error)
    if isinstance(error, urllib.error.HTTPError):
        code = ''
        try:
            detail = json.loads(error.read(8192)).get('error', {})
            code = str(detail.get('code') or detail.get('type') or '') if isinstance(detail, dict) else ''
        except Exception:
            pass
        if code in ('context_length_exceeded', 'context_window_exceeded'):
            return '대화가 모델의 문맥 한도를 초과했습니다. 대화를 줄이거나 더 큰 문맥 모델을 선택하세요.'
        if code in ('no_tool_model', 'tools_not_supported', 'unsupported_tools'):
            return '선택한 모델의 도구 호출을 사용할 수 없습니다. 도구 지원 모델을 선택하세요.'
        if code in ('no_vision_model', 'vision_not_supported'):
            return '선택한 모델의 이미지 입력을 사용할 수 없습니다.'
        return {400: '요청 형식·문맥 길이·모델 기능을 확인하세요.', 401: '통합 API 키가 유효하지 않습니다.',
                403: 'API 키 권한을 확인하세요.', 404: '모델 ID 또는 Base URL 경로를 확인하세요.',
                422: '선택한 모델이 요청한 기능을 지원하지 않거나 사용 가능한 모델이 없습니다.',
                429: '사용 한도에 도달했습니다. 잠시 후 다시 시도하거나 모델을 직접 변경하세요.',
                500: '게이트웨이 또는 상위 제공자 오류입니다.', 502: '상위 제공자 응답 오류입니다.',
                503: '사용 가능한 상위 제공자가 없습니다.', 504: '상위 제공자 응답 시간이 초과됐습니다.'}.get(error.code, '게이트웨이 HTTP 오류입니다.')
    if isinstance(error, TimeoutError):
        return '서버 응답 시간이 초과됐습니다.'
    if isinstance(error, urllib.error.URLError):
        return '서버에 연결할 수 없습니다. 실행 상태와 Base URL을 확인하세요.'
    return '응답 형식이 잘못됐거나 스트림이 완료되지 않았습니다.'


def discover():
    import llm_client
    c = config()
    e = endpoint(PREFIX + c['model'])
    with llm_client.open_url(e['base_url'] + '/models', timeout=min(c['timeout'], 10),
                             headers=llm_client.request_headers(e)) as response:
        result = json.load(response)
    if not isinstance(result, dict) or not isinstance(result.get('data'), list):
        raise ValueError('Invalid models response')
    ids = []
    for row in result['data'][:5000]:
        try:
            model = validate_model(row['id'])
        except (ValueError, KeyError, TypeError):
            continue
        ids.append(model)
    with _lock:
        _catalogs[identity()] = sorted(set(ids))
    return ids


def public():
    with _lock:
        c = config()
        return {k: c[k] for k in ('enabled', 'base_url', 'model', 'timeout')} | {
            'has_key': bool(c['key']), 'models': list(_catalogs.get(identity(), [])),
            'report': dict(_reports.get(identity(), {'chat': 'untested', 'streaming': 'untested', 'tools': 'untested', 'vision': 'untested'}))}


def menu():
    try:
        c = public()
        if not c['enabled']:
            return []
        return [{'name': PREFIX + m, 'label': 'FreeLLMAPI · ' + m}
                for m in dict.fromkeys([c['model'], 'auto', *c['models']])]
    except (ValueError, OSError):
        return []


def probe():
    """Only harmless synthetic prompts. Test actual stream assembly + local tool result.

    Results apply to this URL/key/model configuration at test time, not every
    model behind auto; no production memories, camera images or real tools used.
    """
    import llm_client
    if not _probe_lock.acquire(blocking=False):
        raise ValueError('FreeLLMAPI 연결 테스트가 이미 진행 중입니다.')
    try:
        c = config()
        fingerprint = identity()
        selected = PREFIX + c['model']
        report = {'checked_at': time.time(), 'model': c['model'], 'reachable': False,
                  'authentication': 'untested', 'models': 'untested', 'chat': 'untested',
                  'streaming': 'untested', 'tools': 'untested', 'vision': 'untested', 'errors': {}}
        def check(name, action):
            try:
                result = action()
                report[name] = 'verified'
                report['reachable'] = True
                report['authentication'] = 'accepted'
                return result
            except Exception as error:
                if isinstance(error, urllib.error.HTTPError):
                    report['reachable'] = True
                    if error.code in (401, 403): report['authentication'] = 'rejected'
                report[name] = 'failed'
                report['errors'][name] = error_reason(error)
                return None
        check('models', discover)
        history = [{'role': 'system', 'content': 'You are AIRI. Follow the user request concisely.'},
                   {'role': 'user', 'content': 'Reply with READY.'}]
        def chat():
            e = endpoint(selected)
            body = {'model': e['model'], 'messages': history, 'stream': False, 'max_tokens': 512}
            with llm_client.open_url(e['base_url'] + '/chat/completions', body, e['response_timeout'], llm_client.request_headers(e)) as r:
                obj = json.load(r)
            answer = obj['choices'][0]
            if answer.get('finish_reason') == 'length' or not isinstance(answer['message'].get('content'), str) or not answer['message']['content'].strip():
                raise ValueError('Invalid completion')
        check('chat', chat)
        check('streaming', lambda: list(llm_client.stream(history, selected, 512)))
        def tool_roundtrip():
            tool = {'type': 'function', 'function': {'name': 'airi_probe', 'description': 'Get the local verification code. You must call this tool to learn it.',
                    'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}
            messages = [{'role': 'system', 'content': 'You are AIRI. Call airi_probe before answering. Never invent its result.'},
                        {'role': 'user', 'content': 'Call airi_probe and tell me its verification code.'}]
            first = next((p['message'] for p in llm_client.tool_stream(messages, selected, [tool], 1024) if p['type'] == 'message'), None)
            calls = first.get('tool_calls', []) if first else []
            if len(calls) != 1 or calls[0]['function']['name'] != 'airi_probe' or json.loads(calls[0]['function']['arguments'] or '{}') != {}:
                raise ValueError('Tool call not verified')
            code = 'AIRI-' + secrets.token_hex(4)
            messages += [first, {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': json.dumps({'verification_code': code})}]
            final = llm_client.complete(messages, selected, 512)
            if code not in final:
                raise ValueError('Tool result not verified')
        check('tools', tool_roundtrip)
        with _lock:
            # A slow check must never mark a newer configuration as verified.
            if identity() != fingerprint:
                raise ProviderError('테스트 중 설정이 바뀌었습니다. 저장된 설정으로 다시 검사하세요.')
            _reports[fingerprint] = report
        return report
    finally:
        _probe_lock.release()

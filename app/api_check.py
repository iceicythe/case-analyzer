"""One small provider request, independent of datasets and review queues."""
import json

from app.candidate_review import create_candidate_adapter
from app.model_runner import ModelConfigurationError


def check_api(adapter_factory=None):
    """Never return provider exception text, credentials or raw model output."""
    adapter = None
    try:
        adapter = (adapter_factory or create_candidate_adapter)()
        response = adapter.client.chat.completions.create(
            model=adapter.config.model,
            messages=[{'role':'user','content':'连接检查。请只输出 JSON 对象：{"ok":true}'}],
            response_format={'type':'json_object'},
            extra_body={'thinking':{'type':'disabled'}},
            max_tokens=128, timeout=30, stream=False)
        choices = getattr(response,'choices',None)
        if not choices or choices[0].finish_reason == 'length':
            return {'ok':False,'code':'invalid_response','message':'服务已响应，但返回内容缺失或被截断。'}
        content = choices[0].message.content
        try:
            result = json.loads(content)
        except (ValueError,TypeError):
            result = None
        if not isinstance(result,dict) or result.get('ok') is not True:
            return {'ok':False,'code':'invalid_response','message':'服务已响应，但未返回要求的 JSON。'}
        return {'ok':True,'code':'connected','message':'API 连接成功，模型可返回 JSON。重启工作台后启用自动复核。'}
    except ModelConfigurationError as error:
        return {'ok':False,'code':'configuration_error','message':str(error)}
    except Exception as error:
        status = getattr(error,'status_code',None)
        code, message = 'connection_error','连接失败，请检查地址、网络及服务状态。'
        if status == 401:
            code, message = 'authentication_error','密钥无效或无权限，请检查 api_key。'
        elif status == 402:
            code, message = 'balance_error','账号余额不足，请检查服务商账户。'
        elif status == 403:
            code, message = 'permission_error','账号无此服务或模型的访问权限。'
        elif status == 429:
            code, message = 'rate_limit','请求受限，请检查额度或稍后再试。'
        elif status in {400,404,422}:
            code, message = 'request_error','服务不接受当前模型或参数，请检查 model 和 base_url。'
        elif type(error).__name__ in {'TimeoutError','APITimeoutError'}:
            code, message = 'timeout','连接检查超过 30 秒，请检查网络或稍后再试。'
        return {'ok':False,'code':code,'message':message}
    finally:
        if adapter:
            close = getattr(adapter.client,'close',None)
            if close:
                try:
                    close()
                except Exception:
                    pass

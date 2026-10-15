"""Check real-provider probe contracts without making network requests."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.api_check import check_api
from app.candidate_review import create_candidate_adapter


class ApiCheckTests(unittest.TestCase):
    def adapter(self, content='{"ok":true}', finish_reason='stop'):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(finish_reason=finish_reason,message=SimpleNamespace(content=content))])
        return SimpleNamespace(config=SimpleNamespace(model='deepseek-flash'),client=client)

    def test_empty_key_does_not_make_request(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'configuration.json'
            path.write_text('{"api_key":""}',encoding='utf-8')
            with patch.dict('os.environ',{'DEEPSEEK_API_KEY':''}), patch('app.candidate_review.DeepSeekAdapter') as adapter:
                result = check_api(lambda:create_candidate_adapter(path))
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'],'configuration_error')
                adapter.assert_not_called()

    def test_connection_probe_is_bounded_and_closes_client(self):
        adapter = self.adapter()
        self.assertTrue(check_api(lambda:adapter)['ok'])
        arguments = adapter.client.chat.completions.create.call_args.kwargs
        self.assertEqual(arguments['max_tokens'],128)
        self.assertEqual(arguments['timeout'],30)
        self.assertEqual(arguments['extra_body'],{'thinking':{'type':'disabled'}})
        self.assertEqual(arguments['response_format'],{'type':'json_object'})
        adapter.client.chat.completions.create.assert_called_once()
        adapter.client.close.assert_called_once()

    def test_provider_errors_are_distinguished_and_secret_is_not_printed(self):
        for status,code in ((401,'authentication_error'),(402,'balance_error'),(429,'rate_limit'),(400,'request_error')):
            with self.subTest(status=status):
                adapter = self.adapter()
                error = RuntimeError('private-test-key-do-not-print')
                error.status_code = status
                adapter.client.chat.completions.create.side_effect = error
                result = check_api(lambda:adapter)
                self.assertEqual(result['code'],code)
                self.assertNotIn('private-test-key',json.dumps(result))
                adapter.client.close.assert_called_once()

    def test_truncated_or_invalid_json_is_not_connection_success(self):
        for content,reason in (('not json','stop'),('{"ok":false}','stop'),('{"ok":true}','length')):
            with self.subTest(content=content,reason=reason):
                adapter = self.adapter(content,reason)
                self.assertEqual(check_api(lambda:adapter)['code'],'invalid_response')

    def test_local_settings_are_used_without_leaking_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'configuration.json'
            path.write_text(json.dumps({'api_key':'private-fixture','model':'configured-model','max_retries':0}),encoding='utf-8')
            with patch('app.candidate_review.DeepSeekAdapter') as adapter:
                create_candidate_adapter(path)
                self.assertEqual(adapter.call_args.kwargs['config'].model,'configured-model')
                self.assertEqual(adapter.call_args.kwargs['api_key'],'private-fixture')


if __name__ == '__main__':
    unittest.main()

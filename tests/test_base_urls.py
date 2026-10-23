import unittest
from connections import normalize_base, suggest_base_url


class BaseUrlHintTests(unittest.TestCase):
    def test_known_endpoint_suffixes_preserve_provider_and_proxy_prefixes(self):
        cases = {
            'https://api.deepseek.com/chat/completions/v1': 'https://api.deepseek.com',
            'https://api.deepseek.com/chat/completions/': 'https://api.deepseek.com',
            'https://api.deepseek.com/models': 'https://api.deepseek.com',
            'https://api.deepseek.com/beta/chat/completions': 'https://api.deepseek.com/beta',
            'https://proxy.test/custom/v1/chat/completions': 'https://proxy.test/custom/v1',
            'https://proxy.test/custom/chat/completions/v1': 'https://proxy.test/custom/v1',
            'https://proxy.test/custom/v1/models': 'https://proxy.test/custom/v1',
            'https://proxy.test/custom/v1/responses': 'https://proxy.test/custom/v1',
            'https://workspace.aliyuncs.com/compatible-mode/v1/chat/completions': 'https://workspace.aliyuncs.com/compatible-mode/v1',
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(suggest_base_url(original), expected)
                self.assertEqual(normalize_base(original), original.rstrip('/'))

    def test_valid_bases_and_invalid_sensitive_urls_have_no_guess(self):
        for base in ['https://api.deepseek.com', 'https://api.deepseek.com/v1',
                     'https://proxy.test/custom/v1', 'https://proxy.test/models/gateway',
                     'https://name:secret@proxy.test/v1/models', 'https://proxy.test/v1/models?key=secret',
                     '', None, 'ftp://proxy.test/models']:
            with self.subTest(base=base):self.assertIsNone(suggest_base_url(base))

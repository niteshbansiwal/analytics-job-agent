"""Regression tests for original LinkedIn URLs and legacy recovery."""
import unittest
from unittest.mock import patch

from app.linkedin_urls import linkedin_profile_url


class LinkedInURLTests(unittest.TestCase):
    def test_country_specific_profile_is_preserved(self):
        original = 'https://in.linkedin.com/in/shruti-sinha-61a591149'
        self.assertEqual(linkedin_profile_url(original), original)

    def test_other_linkedin_subdomains_are_not_rewritten(self):
        original = 'https://uk.linkedin.com/in/example-person-123?trk=public_profile'
        self.assertEqual(linkedin_profile_url(original), original)

    def test_standard_profile_is_preserved(self):
        original = 'https://www.linkedin.com/in/yashaswini-s-achar-1650a7172'
        self.assertEqual(linkedin_profile_url(original), original)

    def test_reject_unsafe_hosts_and_schemes(self):
        for url in (
            'http://in.linkedin.com/in/person',
            'https://in.linkedin.com.evil.example/in/person',
            'https://example.com/in/person',
            'https://user:pass@linkedin.com/in/person',
            'https://127.0.0.1/in/person',
            'https://linkedin.com/',
            'javascript:alert(1)',
        ):
            with self.subTest(url=url):
                self.assertEqual(linkedin_profile_url(url), '')

    def test_rocketreach_receives_original_url(self):
        from app.integrations import rocketreach_lookup
        original = 'https://in.linkedin.com/in/shruti-sinha-61a591149'
        with patch('app.integrations._rocketreach_request',
                   return_value={'status': 'not_found'}) as request:
            rocketreach_lookup(original)
            self.assertEqual(request.call_args.kwargs['params']['linkedin_url'], original)


if __name__ == '__main__':
    unittest.main()

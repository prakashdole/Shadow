import unittest
from unittest.mock import Mock

from tools.obscura_pair import INBOX, INSTAGRAM, instagram_cookies


class ObscuraPairTest(unittest.TestCase):
    def test_copies_only_unpartitioned_instagram_cookies_with_their_scope(self):
        cookie = {
            "name": "dummy", "value": "fixture", "domain": ".instagram.com", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax", "session": False,
            "expires": 2_000_000_000.5,
        }
        chrome = Mock()
        chrome.command.return_value = {"cookies": [
            cookie,
            dict(cookie, name="host-only", domain="www.instagram.com", session=True),
            dict(cookie, domain=".example.com"),
            dict(cookie, partitionKey={"topLevelSite": "https://example.com"}),
        ]}

        domain_cookie, host_cookie = instagram_cookies(chrome)

        chrome.command.assert_called_once_with("Network.getCookies", urls=[INSTAGRAM + "/", INBOX])
        self.assertEqual(domain_cookie, {
            "name": "dummy", "value": "fixture", "domain": ".instagram.com", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": 2_000_000_000,
        })
        self.assertEqual(host_cookie, {
            "name": "host-only", "value": "fixture", "url": INSTAGRAM + "/", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax",
        })

"""Offline lifecycle checks for the private live-DOM diagnostic."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from shadow.contracts import InboxResult, ReadResult
from tools import live_dom


class _FailedBrowser:
    instance = None

    def __init__(self, **_kwargs):
        type(self).instance = self
        self.page = None
        self.closed = False

    async def start(self):
        return InboxResult('dom_unknown', detail='cdp_connect_failed:SecretError')

    async def close(self):
        self.closed = True


class _KnownStartupBrowser(_FailedBrowser):
    async def start(self):
        return InboxResult('dom_unknown', detail='owned_browser_navigation_dns_failed')


class _Page:
    url = 'https://www.instagram.com/direct/t/12345/'
    context = type('Context', (), {'browser': type('Browser', (), {'version': 'test'})()})()
    viewport_size = {'width': 1, 'height': 1}

    async def evaluate(self, _source):
        return {}

    async def screenshot(self, **_kwargs):
        raise RuntimeError('capture_failed')


class _HistoryBrowser:
    def __init__(self, **_kwargs):
        self.page = _Page()

    async def start(self):
        return InboxResult('ok')

    def _assert_on_site(self):
        pass

    async def read_thread(self, _thread, want):
        return ReadResult('ok', events=(), coverage={'requested_count': want})

    async def close(self):
        pass


class TestLiveDomAudit(unittest.TestCase):
    def _run(self, browser, commands):
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch) / 'evidence'
            stream = io.StringIO()
            with (patch.object(live_dom, 'InstagramBrowser', browser),
                  patch.object(live_dom.sys, 'stdin', io.StringIO(commands)),
                  redirect_stdout(stream)):
                asyncio.run(live_dom.audit(Path(scratch) / 'profile', output))
            return ([json.loads(line) for line in stream.getvalue().splitlines()],
                    sorted(item.name for item in output.iterdir()))

    def test_failed_start_closes_without_page_or_private_detail(self):
        output, evidence = self._run(_FailedBrowser, '{"action":"capture"}\n')
        self.assertEqual(output, [{'started': 'dom_unknown'}])
        self.assertEqual(evidence, [])
        self.assertTrue(_FailedBrowser.instance.closed)

    def test_failed_start_reports_only_an_allowlisted_safe_detail(self):
        output, evidence = self._run(_KnownStartupBrowser, '')
        self.assertEqual(output, [{
            'started': 'dom_unknown', 'detail': 'owned_browser_navigation_dns_failed',
        }])
        self.assertEqual(evidence, [])
        self.assertTrue(_KnownStartupBrowser.instance.closed)

    def test_failed_capture_cannot_reuse_a_history_evidence_name(self):
        output, evidence = self._run(
            _HistoryBrowser,
            '{"action":"capture"}\n{"action":"history","want":1}\n'
            '{"action":"history","want":1}\n{"action":"close"}\n',
        )
        self.assertEqual(output[1], {'error_type': 'RuntimeError'})
        self.assertEqual([item['history_status'] for item in output if 'history_status' in item], ['ok', 'ok'])
        self.assertEqual(evidence, ['history-002.json', 'history-004.json'])

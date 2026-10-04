"""POST /parse rejects non-text bodies with 400 (never a 500)."""

import asyncio

import pytest
from fastapi import HTTPException

from order_parser.api.jobs import parse_endpoint


class _FakeRequest:
    def __init__(self, body):
        from types import SimpleNamespace

        self._body = body
        self.app = SimpleNamespace(state=SimpleNamespace(job_store=None, pipeline=None,
                                                         job_queue=None))

    async def json(self):
        return self._body


def test_parse_rejects_dict_text_with_400():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(parse_endpoint(_FakeRequest({"text": {"nested": "object"}}), file=None))
    assert exc_info.value.status_code == 400


def test_parse_rejects_missing_text_with_400():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(parse_endpoint(_FakeRequest({}), file=None))
    assert exc_info.value.status_code == 400

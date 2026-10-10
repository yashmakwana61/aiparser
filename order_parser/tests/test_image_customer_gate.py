"""Image orders need a resolved customer before awaiting confirmation.

Regression test for ORD-20261010-000011: an image with all products valid
but no customer was parked in "pending" (confirm could only fail with
customer_not_resolved, and the case showed zero issues). Images now go to
review unless products AND customer are valid.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import telegram.error

from order_parser.channels import telegram_handler as tg_module
from order_parser.channels.telegram_handler import is_not_modified_error
from order_parser.config import Settings
from order_parser.models import CustomerModel, ItemModel, MetadataModel, OrderModel, ParsedOrder
from order_parser.services.pipeline import OrderPipeline


class FakeOdoo:
    enabled = True

    def fetch_product_catalog(self):
        return [{"id": 2410, "name": "Focaccia Bread"}]

    def find_partner(self, customer):
        return None

    def create_partner(self, customer):
        return 99

    def create_sale_order(self, partner_id, items, notes=""):
        return {"id": 1, "name": "SO00001"}


def _pipeline(**overrides):
    defaults = {"auto_create_products": False, "auto_create_all_orders": False}
    defaults.update(overrides)
    return OrderPipeline(FakeOdoo(), settings=Settings(**defaults))


def _parsed(customer: str, product: str = "Focaccia Bread") -> ParsedOrder:
    items = [] if product == "" else [ItemModel(product_name=product, quantity=2)]
    return ParsedOrder(order=OrderModel(
        customer=CustomerModel(name=customer),
        items=items,
        metadata=MetadataModel(confidence=95.0),
    ), ai_response={})


def test_image_missing_customer_goes_to_review_not_pending():
    result = _pipeline().process("telegram", "image", _parsed(customer=""), {"chat_id": 1})
    assert result["status"] == "review"
    assert result["readiness_status"] == "MISSING_CUSTOMER"
    # Confirming a review order fails cleanly instead of raising into logs.
    denied = _pipeline().confirm_order(result["order_id"])
    # Fresh pipeline has no pending store entry; emulate stored record path:
    assert denied["status"] == "error"


def test_image_missing_customer_confirm_refused_on_stored_record():
    pipeline = _pipeline()
    result = pipeline.process("telegram", "image", _parsed(customer=""), {"chat_id": 1})
    assert result["status"] == "review"
    refused = pipeline.confirm_order(result["order_id"], actor="telegram:123")
    assert refused["status"] == "error"
    assert "review" in refused["message"]


def test_image_valid_customer_still_awaits_confirmation():
    pipeline = _pipeline()
    result = pipeline.process("telegram", "image", _parsed(customer="ABC"))
    assert result["status"] == "pending"
    confirmed = pipeline.confirm_order(result["order_id"], actor="telegram")
    assert confirmed["status"] == "success"


def test_image_with_no_items_goes_to_review():
    result = _pipeline().process("telegram", "image", _parsed(customer="ABC", product=""), {"chat_id": 1})
    assert result["status"] == "review"


# ------------------------------------------------- telegram edit no-op guard


class NotModifiedMessage:
    def __init__(self):
        self.replies: list[str] = []
        self.edits = 0

    async def edit_text(self, text, reply_markup=None):
        self.edits += 1
        raise telegram.error.BadRequest("Message is not modified: specified new message "
                                        "content and reply markup are exactly the same")

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)


def test_not_modified_edit_skips_reply_fallback():
    async def fail_edit(factory):
        return await factory()

    async def record_reply(text, **kwargs):
        fake_self.replies.append(text)

    async def record_follow_up(query, outcome):
        fake_self.follow_ups.append(outcome)

    fake_self = SimpleNamespace(_with_retry=fail_edit, _reply=record_reply,
                                _send_follow_up=record_follow_up,
                                replies=[], follow_ups=[])
    query = SimpleNamespace(message=NotModifiedMessage())
    outcome = {"text": "Review", "keyboard": None, "edit": True}

    asyncio.run(tg_module.TelegramHandler._apply_case_outcome(fake_self, query, outcome))

    assert query.message.edits == 1
    assert fake_self.replies == [], "no duplicate reply on a no-op edit"
    assert len(fake_self.follow_ups) == 1


def test_is_not_modified_error_matches_only_that_case():
    assert is_not_modified_error(telegram.error.BadRequest("Message is not modified"))
    assert not is_not_modified_error(telegram.error.BadRequest("Message to edit not found"))
    # Duck-typed on the message text so wrapped/derived errors match too.
    assert is_not_modified_error(ValueError("message is not modified"))

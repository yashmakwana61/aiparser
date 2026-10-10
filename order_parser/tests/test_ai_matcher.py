"""AI matcher: selection-only LLM tiebreaker with hallucination guards."""

from order_parser.ai.matcher import AIMatcher
from order_parser.config import Settings


def _settings(**overrides):
    base = dict(ai_match_enabled=True, ai_match_model="gpt-4.1-mini",
                ai_match_attempts=2, ai_match_max_candidates=8,
                ai_match_auto_threshold=95.0)
    base.update(overrides)
    return Settings(**base)


def _client(responses):
    calls = []

    def fake(args):
        calls.append(args)
        response = responses.pop(0) if responses else "{}"
        if isinstance(response, Exception):
            raise response
        return response

    fake.calls = calls
    return fake


def test_match_products_parses_picks_and_validates_ids():
    client = _client(['{"picks": ['
                      '{"index": 0, "product_id": 753, "confidence": 97, "reason": "pack match"},'
                      '{"index": 1, "product_id": 999999, "confidence": 99, "reason": "bogus"}'
                      ']}'])
    matcher = AIMatcher(client=client, settings=_settings())
    out = matcher.match_products([
        {"index": 0, "raw_name": "Kulcha Plain 6pcs", "quantity": 6,
         "candidates": [{"id": 753, "name": "KULCHA BREAD (6 Pcs)"},
                        {"id": 755, "name": "KULCHA BREAD MINI"}]},
        {"index": 1, "raw_name": "Pav", "quantity": 2,
         "candidates": [{"id": 2549, "name": "Pav 250gm"}]},
    ])
    assert set(out) == {0}
    assert out[0]["product_id"] == 753
    assert out[0]["confidence"] == 97.0
    # One batched call, mini model, temp 0.
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "gpt-4.1-mini"
    assert client.calls[0]["temperature"] == 0


def test_match_products_retries_invalid_json_then_gives_up():
    client = _client(['not json', 'still not json'])
    matcher = AIMatcher(client=client, settings=_settings())
    assert matcher.match_products([{"index": 0, "raw_name": "X",
                                    "candidates": [{"id": 1, "name": "Y"}]}]) == {}
    assert len(client.calls) == 2


def test_match_products_transport_failure_returns_empty():
    client = _client([RuntimeError("gateway down")])
    matcher = AIMatcher(client=client, settings=_settings())
    assert matcher.match_products([{"index": 0, "raw_name": "X",
                                    "candidates": [{"id": 1, "name": "Y"}]}]) == {}


def test_rank_customers_orders_ids_and_ignores_unknown():
    client = _client(['{"ranking": ['
                      '{"partner_id": 2, "confidence": 92, "reason": "unit"},'
                      '{"partner_id": 4242, "confidence": 99, "reason": "hallucinated"},'
                      '{"partner_id": 1, "confidence": 80, "reason": "chain"}'
                      ']}'])
    matcher = AIMatcher(client=client, settings=_settings())
    out = matcher.rank_customers("Grand Hotel Airport", [
        {"partner_id": 1, "name": "GRAND HOTEL NOIDA"},
        {"partner_id": 2, "name": "GRAND SAKET SUITES"},
    ])
    assert out == [2, 1]


def test_rank_customers_failure_returns_empty():
    client = _client([RuntimeError("boom")])
    matcher = AIMatcher(client=client, settings=_settings())
    assert matcher.rank_customers("X", [{"partner_id": 1, "name": "Y"},
                                        {"partner_id": 2, "name": "Z"}]) == []


def test_matcher_disabled_by_default():
    assert AIMatcher(settings=Settings()).enabled is False

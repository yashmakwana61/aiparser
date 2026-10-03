import json, sys, time
sys.path.insert(0, ".")
from order_parser.ai.text_parser import TextParser
from order_parser.config import get_settings

s = get_settings()
print("model=%s token=%s" % (s.ai_text_model, "set" if s.puter_auth_token else "MISSING"))

text = (
    "Order PO-7841 dated 2026-09-28. Customer Acme Traders, acme@example.com. "
    "Items: 10 x Industrial Keyboard @ 1450 INR each, "
    "5 x Optical Mouse @ 450 INR each. Delivery 2026-10-10."
)
t0 = time.monotonic()
parsed = TextParser().parse(text)
print("PARSE_OK in %.1fs" % (time.monotonic() - t0))
print(json.dumps(parsed, indent=2, ensure_ascii=False, default=str))

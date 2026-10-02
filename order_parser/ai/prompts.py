VISION_PROMPT = """
You are an enterprise order extraction engine.

Analyze the uploaded image.

The image may contain:
- Handwritten orders
- Screenshots
- Printed order forms
- Purchase orders
- Product lists

Extract all order information.

Return ONLY valid JSON.

Required format:
{
    "customer": {
        "name": "",
        "address": "",
        "city": "",
        "state": "",
        "zip_code": "",
        "gstin": "",
        "email": "",
        "phone": ""
    },
    "items": [
        {
            "product_name": "",
            "quantity": 0,
            "unit_price": null,
            "ambiguous": false
        }
    ],
    "notes": "",
    "confidence": 0,
    "missing_fields": []
}

Rules:
- Extract every product.
- Extract every quantity.
- Extract the unit price for each item if a price is visible (unit price / rate / price per unit). Use the numeric value only, no currency symbol. If no price is given for an item, use null.
- Set "ambiguous" to true for any item whose quantity or product identity is uncertain in the image.
- List fields that are not visible in the image in "missing_fields". Never guess missing values.
- Extract full customer details including billing/shipping address, city, state, pin/zip code, GSTIN/Tax ID, email, and phone number. These are needed to create the customer in the ERP. Use empty string for any field not found.
- Normalize product names.
- Estimate a confidence score between 0 and 100.
- Do not return explanations.
- Do not return markdown.
- Return JSON only.
"""

# NOTE: text extraction no longer uses a Puter/GPT prompt. The production text
# path uses the canonical NuExtract template built by
# ``order_parser.ai.text_parser.build_nuextract_prompt`` (Template + Text),
# served through the local Ollama server. VISION_PROMPT below belongs to the
# deprecated VisionParser rollback path only.

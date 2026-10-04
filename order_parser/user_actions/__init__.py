"""User-facing order-case layer: internal diagnostics -> actionable UX.

Pipeline: existing parser/resolver output -> action resolver ->
UserActionRequired -> channel renderer (Telegram) -> user correction ->
correction service -> deterministic re-resolve -> continue existing flow.
"""

from order_parser.user_actions import models  # noqa: F401

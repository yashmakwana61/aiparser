from __future__ import annotations

import structlog

from order_parser.sessions.models import StaffIdentity

logger = structlog.get_logger(__name__)


class StaffRegistry:
    """Maps authorized Telegram user IDs to internal staff identities.

    Configuration format (AUTHORIZED_STAFF):
        "123456789:bob_ops, 987654321:Alice Sales"

    When no entries are configured the registry is not enforced: every sender
    is treated as anonymous staff and no authorization gate is applied. This
    keeps development environments working; production must configure it.
    """

    def __init__(self, raw: str = "") -> None:
        self._staff: dict[int, StaffIdentity] = {}
        for entry in raw.replace("\n", ",").replace(";", ",").split(","):
            entry = entry.strip()
            if not entry:
                continue
            user_id, sep, name = entry.partition(":")
            name = name.strip()
            try:
                numeric_id = int(user_id.strip())
            except ValueError:
                logger.warning("staff_registry.invalid_entry", entry=entry)
                continue
            if not sep or not name:
                logger.warning("staff_registry.invalid_entry", entry=entry)
                continue
            self._staff[numeric_id] = StaffIdentity(
                telegram_user_id=numeric_id,
                staff_id=name,
                display_name=name.replace("_", " ").replace("-", " ").title(),
            )

    @property
    def enforced(self) -> bool:
        return bool(self._staff)

    @property
    def staff_ids(self) -> list[str]:
        return sorted(identity.staff_id for identity in self._staff.values())

    def resolve(self, telegram_user_id: int | None) -> StaffIdentity | None:
        if telegram_user_id is None:
            return None
        return self._staff.get(int(telegram_user_id))

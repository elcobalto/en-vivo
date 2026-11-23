"""How much of the database plan En Vivo uses: storage against the plan's limit, the biggest tables, open
connections, and how fast it grows (for the admins' page)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

WARN_AT = 70  # percent of the plan used: amber
DANGER_AT = 85  # red


@dataclass
class Table:
    name: str
    bytes: int
    rows: int


@dataclass
class Usage:
    vendor: str
    bytes: int
    limit_bytes: int
    tables: list[Table] = field(default_factory=list)
    connections: int | None = None
    max_connections: int | None = None
    version: str = ""
    history: list[tuple] = field(default_factory=list)  # (day, bytes), oldest first
    per_day: float | None = None  # average growth, bytes a day
    days_left: int | None = None  # at that pace, until the plan is full

    @property
    def percent(self) -> float:
        return round(100 * self.bytes / self.limit_bytes, 2) if self.limit_bytes else 0

    @property
    def level(self) -> str:
        return "danger" if self.percent >= DANGER_AT else "warning" if self.percent >= WARN_AT else "success"

    @property
    def connections_percent(self) -> int:
        return round(100 * self.connections / self.max_connections) if self.connections and self.max_connections else 0

    @property
    def history_max(self) -> int:
        return max((b for _, b in self.history), default=0)


def usage() -> Usage:
    if connection.vendor == "postgresql":
        result = _postgres()
    else:  # local SQLite: the file's size; there is no plan limit to speak of
        path = settings.DATABASES["default"]["NAME"]
        result = Usage("sqlite", os.path.getsize(path) if os.path.exists(str(path)) else 0, 0)
    _record(result.bytes)
    _growth(result)
    return result


def _postgres() -> Usage:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database()), current_setting('server_version'), "
                       "current_setting('max_connections')::int, "
                       "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())")
        size, version, max_connections, connections = cursor.fetchone()
        cursor.execute("""
            SELECT c.relname, pg_total_relation_size(c.oid), GREATEST(c.reltuples, 0)::bigint
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname = 'public'
            ORDER BY 2 DESC LIMIT 10""")
        tables = [Table(name, size_, rows) for name, size_, rows in cursor.fetchall()]
    return Usage("postgresql", size, settings.DATABASE_STORAGE_LIMIT_MB * 1024 * 1024, tables, connections,
                 max_connections, version.split()[0])


def _record(size: int) -> None:
    """Today's size, kept once a day (the latest reading of the day wins)."""
    from .models import DatabaseSize

    today = timezone.localdate()
    try:
        with transaction.atomic():
            if not DatabaseSize.objects.filter(day=today).update(bytes=size):
                DatabaseSize.objects.create(day=today, bytes=size)
    except IntegrityError:  # two admins at once: one of them wrote it
        pass


def _growth(result: Usage) -> None:
    """The last 30 days' sizes, the average growth per day, and how long the plan lasts at that pace."""
    from .models import DatabaseSize

    since = timezone.localdate() - timedelta(days=30)
    result.history = list(DatabaseSize.objects.filter(day__gte=since).values_list("day", "bytes"))
    if len(result.history) < 2:
        return
    (first_day, first), (last_day, last) = result.history[0], result.history[-1]
    result.per_day = (last - first) / max((last_day - first_day).days, 1)
    if result.per_day > 0 and result.limit_bytes:
        result.days_left = max(int((result.limit_bytes - result.bytes) / result.per_day), 0)

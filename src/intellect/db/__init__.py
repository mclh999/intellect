"""SQLite 连接与新库初始化。"""

from intellect.db.connection import ALIAS_COLLATION, alias_comparison_key, connect_database
from intellect.db.initialize import DatabaseInitializationError, initialize_database

__all__ = [
    "ALIAS_COLLATION",
    "DatabaseInitializationError",
    "alias_comparison_key",
    "connect_database",
    "initialize_database",
]

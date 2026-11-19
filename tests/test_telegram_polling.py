from app.telegram_polling import get_persistent_state, set_persistent_state
from app.db import MemoryStore


def test_polling_cursor_and_pause_state_persist_in_memory_store():
    store = MemoryStore()
    assert get_persistent_state(store, "telegram_update_offset", 0) == 0
    set_persistent_state(store, "telegram_update_offset", 123)
    set_persistent_state(store, "paused", True)
    assert get_persistent_state(store, "telegram_update_offset", 0) == 123
    assert get_persistent_state(store, "paused", False) is True

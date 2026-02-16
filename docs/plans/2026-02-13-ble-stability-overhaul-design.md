# BLE Stability Overhaul Design

**Date:** 2026-02-13
**Status:** Approved
**Issues addressed:** #101 (connection hangs), #87 (permanent skipping), PR #99 (concurrency audit)

## Problem

Post-v1.1.17 stability regressions introduced by well-intentioned community fixes:

- **PR #85 (stability issues):** Added `_connection_failures` counter with max-5 cap that permanently disables polling. Added `_background_reconnect()` and `_on_device_disconnect()`.
- **PR #95 (disconnect churn fix):** Removed the disconnect callback invocation to stop aggressive reconnect loops, but left `_background_reconnect()` and `_on_device_disconnect()` as dead code. Now the failure counter only increments from `_async_update_data()` and never resets once it hits 5.
- **PR #99 (asyncio.Lock):** Correctly replaced boolean flag with lock on `connect()`, but `disconnect()` is not protected — race condition between connect/disconnect.

v1.1.17 was stable because it had no failure cap, no background reconnect task, and always attempted connection on every poll cycle.

## Design Principles

1. **Always attempt connection** — no poll cycle should ever be skipped
2. **Lazy reconnection** — reconnect on next poll, not via background tasks
3. **Hard timeouts on everything** — no BLE operation can hang indefinitely
4. **Lock protects all connection state mutations** — connect AND disconnect

## Changes by File

### `devices/base_device.py`

**Keep:** `asyncio.Lock` on `connect()`, `_with_disconnect_on_error()`, `validate_connection()`, simple `_handle_disconnect` (log + clear client).

**Fix:**
- Wrap `establish_connection()` in `asyncio.wait_for(timeout)` — hard outer timeout
- Protect `disconnect()` with `_connect_lock`
- Remove dead `set_disconnect_callback()` / `_disconnect_callback` machinery

### `coordinator.py`

**Remove:**
- `_connection_failures` / `_max_connection_failures` / "skip update" early return
- `_background_reconnect()` task
- `_on_device_disconnect()` callback
- `_reconnection_task` management
- Unused `_consecutive_failures`, `_last_error_log_time`, `_error_backoff_delay`

**Restore from v1.1.17:**
- Every poll always attempts connection
- Simple `_safe_connect()` with inline retry

**Add:**
- Adaptive poll interval: track consecutive failed poll cycles, backoff `min(normal * 2^failures, 600)`, reset on any successful sensor read
- Restore `_schedule_refresh()` in `setFastPollMode()`
- Reduce timeouts: 30s device info, 30s config, 20s sensor data

### `coordinator_calima.py` / `coordinator_svensa.py`

- Remove `set_disconnect_callback()` calls from `__init__`
- Keep all other current code (wrapped exceptions, GATT operations)

### `__init__.py`

- No changes needed (30s timeout on initial refresh is fine)

### Entity/config/helper files

- No changes

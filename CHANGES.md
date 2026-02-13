# Changes: BLE Stability Overhaul

Fixes: #101 (connection hangs), #87 (permanent "skipping update"), PR #99 audit

## `devices/base_device.py`

| Change | What was wrong | What changed |
|--------|---------------|-------------|
| Hard outer timeout on `connect()` | `establish_connection()` could hang indefinitely if BLE handshake stalled, holding the adapter slot forever | Wrapped in `asyncio.wait_for(timeout)` — forcefully cancels after timeout |
| Lock-protected `disconnect()` | `disconnect()` ran without `_connect_lock`, racing with `connect()` — could orphan connections or null the client mid-connect | Now acquires `_connect_lock` before touching `_client` |
| 5s timeout on `disconnect()` | `_client.disconnect()` could hang if BLE stack was wedged | Wrapped in `asyncio.wait_for(timeout=5.0)` |
| 5s timeout on `close_stale_connections()` | Could hang inside the lock with no timeout | Wrapped in `asyncio.wait_for(timeout=5.0)` |
| Removed dead `_disconnect_callback` | `set_disconnect_callback()` stored a callback that `_handle_disconnect` never called (dead since PR #95) | Removed `_disconnect_callback`, `set_disconnect_callback()` |
| Reduced retry params | `max_attempts=5`, `retry_interval=1.0` was too aggressive with the outer timeout | Back to v1.1.17 values: `max_attempts=3`, `retry_interval=0.5` |

## `coordinator.py`

| Change | What was wrong | What changed |
|--------|---------------|-------------|
| Removed "skip update" cap | `_connection_failures >= 5` caused permanent early return — no reset path existed, device stuck unavailable forever | Replaced with `_consecutive_poll_failures` that only affects poll interval, never skips |
| Removed `_background_reconnect()` | Dead code since PR #95 removed disconnect callback invocation. Also competed with poll cycle and double-incremented failure counter | Deleted entirely. Reconnection is lazy (next poll cycle) |
| Removed `_on_device_disconnect()` | Never called — the bleak callback (`_handle_disconnect`) doesn't invoke it | Deleted entirely |
| Simplified `_safe_connect()` | Validated twice (existing + new), reset failure counter non-atomically, used variable timeout | 3 attempts with 1/2s backoff, 30s timeout, validates once per attempt |
| Adaptive poll backoff | `setNormalPollMode()` used `_connection_failures` which was broken | Uses `_consecutive_poll_failures` — backs off poll interval on failure, resets on success, capped at 600s |
| Restored `_schedule_refresh()` | `setFastPollMode()` didn't trigger immediate refresh after writes (removed from v1.1.17) | Restored so boost mode toggle gets immediate feedback |
| Reduced timeouts | 45/45/30s per section (120s worst case) blocked HA for too long | 30/30/20s (80s worst case) |

## `coordinator_calima.py` / `coordinator_svensa.py`

| Change | What was wrong | What changed |
|--------|---------------|-------------|
| Removed `set_disconnect_callback()` calls | Called a method that no longer exists on `BaseDevice`, setting a callback that was never invoked | Removed from both `__init__` methods |

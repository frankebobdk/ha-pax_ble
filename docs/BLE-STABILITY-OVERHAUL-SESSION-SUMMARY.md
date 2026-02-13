# BLE Stability Overhaul — Session Summary

**Date:** 2026-02-13
**Branch:** `fix/ble-stability-overhaul` (4 commits ahead of `main`)
**Status:** Implementation complete, not yet merged or submitted upstream

---

## What Was Done

A full stability audit and fix of the ha-pax_ble Home Assistant custom integration. The integration had chronic problems: fans going permanently "Unavailable", connections hanging, BLE adapter slots getting stuck.

### Root Cause Analysis

**v1.1.17 was the last stable version.** Post-v1.1.17 community PRs introduced regressions:

| PR | Intended Fix | Actual Effect |
|----|-------------|---------------|
| PR #85 | Added connection failure tracking + background reconnect | Introduced `_connection_failures` counter with max-5 cap that permanently disables polling — no reset path exists once counter hits 5 |
| PR #95 | Fixed disconnect callback churn (aggressive reconnect loop) | Removed the callback invocation but left `_background_reconnect()` and `_on_device_disconnect()` as dead code. Counter now only increments from `_async_update_data()` and never resets |
| PR #99 | Replaced boolean flag with asyncio.Lock for concurrent connections | Lock only protects `connect()`, not `disconnect()` — race condition remains |

### Design Decision

Restore v1.1.17's always-retry philosophy while keeping the good parts from post-v1.1.17 changes (asyncio.Lock, wrapped exceptions, connection validation, initial refresh timeout). Remove the broken parts (failure counter cap, background reconnect task, dead disconnect callback machinery).

**Core principle:** The poll interval can slow down on failure (backoff), but polling must never stop entirely.

---

## Commits (oldest to newest)

```
3d6b55f fix: harden BLE connection with outer timeout and lock-protected disconnect
f0cd520 fix: remove broken failure cap, restore always-retry polling
9b5f85d fix: remove dead disconnect callback setup from coordinators
9ffefc8 docs: add CHANGES.md summarizing BLE stability overhaul
```

### Commit 1: `base_device.py` (3d6b55f)

- Wrapped `establish_connection()` in `asyncio.wait_for(timeout)` — hard outer timeout prevents hung BLE adapter (fixes #101)
- Protected `disconnect()` with `_connect_lock` — prevents connect/disconnect race (completes PR #99)
- Added 5s timeout on `disconnect()` and `close_stale_connections()`
- Removed dead `set_disconnect_callback()` / `_disconnect_callback`
- Reduced `max_attempts` to 3, `retry_interval` to 0.5 (v1.1.17 values)

### Commit 2: `coordinator.py` (f0cd520)

- Removed `_connection_failures` counter and "skip update" early return (fixes #87)
- Removed dead `_background_reconnect()` and `_on_device_disconnect()`
- Simplified `_safe_connect()` to 3 attempts with 1/2s backoff, 30s timeout
- Added adaptive poll backoff via `_consecutive_poll_failures` — slows retries but never stops
- Reduced section timeouts to 30/30/20s (from 45/45/30s)
- Restored `_schedule_refresh()` in `setFastPollMode()` for immediate write feedback

### Commit 3: `coordinator_calima.py` + `coordinator_svensa.py` (9b5f85d)

- Removed dead `set_disconnect_callback()` calls from both coordinator constructors

### Commit 4: `CHANGES.md` (9ffefc8)

- Detailed changelog of all fixes organized by file

---

## Files Changed

```
custom_components/pax_ble/devices/base_device.py   — connection lifecycle hardening
custom_components/pax_ble/coordinator.py            — polling overhaul (core fix)
custom_components/pax_ble/coordinator_calima.py     — dead code removal (2 lines)
custom_components/pax_ble/coordinator_svensa.py     — dead code removal (2 lines)
CHANGES.md                                          — changelog
```

## Files NOT Changed

All entity files, config_flow, helpers, const, characteristics, `__init__.py`, device drivers (calima.py, svensa.py) — no changes needed.

---

## Known Limitations (Accepted)

These were flagged during code review and deliberately left as-is:

1. **`_handle_disconnect` sets `self._client = None` outside the lock** — safe under asyncio cooperative scheduling (single-threaded event loop), would require bleak to support async callbacks to fix properly
2. **`validate_connection()` clears `_client` outside the lock** — same reason, safe because coordinator is single-threaded
3. **`_schedule_refresh()` is a private HA API** — commonly used by integrations, same as v1.1.17
4. **Backoff saturates quickly** — with 300s normal interval and 600s max, backoff hits the cap after 1 failure. Could use `_fast_poll_interval` as base for smoother ramp in a future iteration

---

## How to Deploy on Home Assistant

Copy these 4 files to your HA instance at `/config/custom_components/pax_ble/`:

```
devices/base_device.py
coordinator.py
coordinator_calima.py
coordinator_svensa.py
```

Then restart Home Assistant. Back up the originals first. HACS updates will overwrite your changes.

---

## How to Resume This Work

```bash
cd ~/Documents/GitHub/ha-pax_ble
git checkout fix/ble-stability-overhaul
```

### Next Steps (not yet done)

- **Submit upstream PR** to the original repo (github.com/PatrickE94/pax_ble or wherever the current upstream is)
- **Test on actual hardware** — verify on a real Calima/Levante/Svensa fan
- **Consider smoother backoff** — use `_fast_poll_interval` as backoff base instead of `_normal_poll_interval`
- **Consider locking `_handle_disconnect`** — schedule lock-protected cleanup via `hass.loop.call_soon_threadsafe` for full thread safety

### Related Files

- `docs/plans/2026-02-13-ble-stability-overhaul-design.md` — approved design document
- `docs/plans/2026-02-13-ble-stability-overhaul.md` — detailed implementation plan
- `CHANGES.md` — per-file changelog

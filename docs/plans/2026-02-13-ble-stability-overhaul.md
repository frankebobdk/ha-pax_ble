# BLE Stability Overhaul Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore v1.1.17's always-retry stability while keeping post-v1.1.17 improvements (asyncio.Lock, wrapped exceptions, connection validation, initial refresh timeout).

**Architecture:** Remove the broken failure-counter-with-cap mechanism that permanently disables polling. Replace with adaptive poll interval backoff that slows retries but never stops trying. Harden BLE connection lifecycle with hard outer timeouts and lock-protected disconnect.

**Tech Stack:** Python asyncio, bleak/bleak-retry-connector, Home Assistant DataUpdateCoordinator

**Validation:** This project has no unit tests. Validate via:
```bash
docker run --rm -v $(pwd)/custom_components:/github/workspace/custom_components ghcr.io/home-assistant/hassfest:latest
```

**Design doc:** `docs/plans/2026-02-13-ble-stability-overhaul-design.md`

---

### Task 1: Harden `base_device.py` Connection Lifecycle

**Files:**
- Modify: `custom_components/pax_ble/devices/base_device.py`

This is the highest-priority fix. It addresses Issue #101 (hung BLE adapter) and completes PR #99 (lock on disconnect).

**Step 1: Add hard outer timeout to `connect()`**

Wrap `establish_connection()` in `asyncio.wait_for()` so a hung BLE handshake is forcefully cancelled. The `timeout` param passed to `establish_connection()` is an internal retry timeout — it doesn't guarantee the call returns within that time. The outer `wait_for` does.

In `connect()`, replace the `establish_connection` call block (lines 89-98) with:

```python
                self._client = await asyncio.wait_for(
                    establish_connection(
                        BleakClientWithServiceCache,
                        device,
                        name=getattr(self, "name", self._mac),
                        disconnected_callback=self._handle_disconnect,
                        use_services_cache=True,
                        max_attempts=3,
                        retry_interval=0.5,
                    ),
                    timeout=timeout,
                )
```

Note: `max_attempts` reduced from 5 to 3, `retry_interval` from 1.0 to 0.5 (closer to v1.1.17 values). The outer `wait_for` is the real safety net.

Also add `asyncio.TimeoutError` to the except block so it's logged distinctly:

```python
            except asyncio.TimeoutError:
                _LOGGER.warning("Connection to %s timed out after %ds", self._mac, timeout)
                self._client = None
                return False
            except Exception as err:
                _LOGGER.warning("Failed to connect %s: %s", self._mac, err)
                self._client = None
                return False
```

**Step 2: Protect `disconnect()` with `_connect_lock`**

Replace the current `disconnect()` method (lines 106-113) with:

```python
    async def disconnect(self) -> None:
        async with self._connect_lock:
            if self._client:
                try:
                    await asyncio.wait_for(self._client.disconnect(), timeout=5.0)
                except Exception as e:
                    _LOGGER.warning("Error disconnecting %s: %s", self._mac, e)
                finally:
                    self._client = None
```

This prevents concurrent connect/disconnect races and adds a 5s timeout so disconnect can't hang.

**Step 3: Remove dead `_disconnect_callback` machinery**

Remove these from `__init__`:
```python
        self._disconnect_callback = None
```

Remove the entire `set_disconnect_callback` method:
```python
    def set_disconnect_callback(self, callback):
        """Set callback to be called when device disconnects unexpectedly."""
        self._disconnect_callback = callback
```

**Step 4: Run hassfest validation**

```bash
docker run --rm -v $(pwd)/custom_components:/github/workspace/custom_components ghcr.io/home-assistant/hassfest:latest
```

Expected: No errors related to pax_ble. Warnings about missing tests are OK.

**Step 5: Commit**

```bash
git add custom_components/pax_ble/devices/base_device.py
git commit -m "fix: harden BLE connection with outer timeout and lock-protected disconnect

- Wrap establish_connection() in asyncio.wait_for() to enforce hard timeout
  (fixes #101: hung BLE adapter that never releases slot)
- Protect disconnect() with _connect_lock to prevent connect/disconnect race
  (completes PR #99 concurrency fix)
- Add 5s timeout on disconnect to prevent hang during cleanup
- Remove dead _disconnect_callback machinery (unused since PR #95)
- Reduce max_attempts to 3 and retry_interval to 0.5 (v1.1.17 values)"
```

---

### Task 2: Overhaul `coordinator.py` — Remove Broken Failure Cap, Restore Always-Retry

**Files:**
- Modify: `custom_components/pax_ble/coordinator.py`

This is the core fix for Issue #87 (permanent "skipping update"). Remove the broken failure counter mechanism and replace with adaptive poll interval backoff.

**Step 1: Remove dead code and broken failure cap from class definition**

Remove these class-level variables (lines 27-29):
```python
    # Error tracking for rate limiting
    _consecutive_failures = 0
    _last_error_log_time = None
    _error_backoff_delay = 0  # seconds
```

In `__init__`, remove these lines (around lines 60-64):
```python
        # Connection management
        self._connection_failures = 0
        self._max_connection_failures = 5
        self._max_backoff = 300  # 5 minutes max backoff
        self._backoff_multiplier = 2
        self._reconnection_task = None
```

And remove the comment (around line 71):
```python
        # Note: disconnect callback will be set up in child classes after _fan is initialized
```

Replace with simpler tracking:
```python
        # Adaptive poll backoff: tracks consecutive failed poll cycles
        self._consecutive_poll_failures = 0
        self._max_backoff = 600  # 10 minutes max poll interval
```

**Step 2: Remove dead methods**

Remove the entire `_on_device_disconnect()` method (lines 119-130):
```python
    async def _on_device_disconnect(self):
        ...
```

Remove the entire `_background_reconnect()` method (lines 132-160):
```python
    async def _background_reconnect(self):
        ...
```

**Step 3: Simplify `disconnect()`**

Replace the current `disconnect()` method (lines 109-117) with:
```python
    async def disconnect(self):
        """Disconnect from device."""
        if self._fan:
            await self._fan.disconnect()
```

No more reconnection task cancellation needed.

**Step 4: Simplify `_safe_connect()`**

Replace the current `_safe_connect()` (lines 162-195) with a simpler version inspired by v1.1.17 but using the lock-protected `connect()` and `validate_connection()`:

```python
    async def _safe_connect(self) -> bool:
        """Connect with retry and validation. Returns True if connected."""
        if not self._fan:
            return False

        # Reuse existing valid connection
        if self._fan.isConnected():
            if await self._fan.validate_connection():
                return True
            _LOGGER.debug("Existing connection to %s failed validation", self.devicename)

        # Try to connect with backoff
        backoff = 1.0
        for attempt in range(1, 4):
            if await self._fan.connect(timeout=30):
                if await self._fan.validate_connection():
                    return True
                _LOGGER.debug("New connection to %s failed validation", self.devicename)
            if attempt < 3:
                _LOGGER.debug("Connect attempt %d to %s failed, retrying in %ds",
                             attempt, self.devicename, backoff)
                await asyncio.sleep(backoff)
                backoff *= 2
        _LOGGER.warning("Failed to connect to %s after 3 attempts", self.devicename)
        return False
```

3 attempts (not 5) with 1/2s backoff. 30s timeout per attempt. Total worst case: ~93s (30+1+30+2+30).

**Step 5: Rewrite `setFastPollMode()` and `setNormalPollMode()`**

Replace `setFastPollMode()`:
```python
    def setFastPollMode(self):
        """Enable fast polling after a successful write."""
        _LOGGER.debug("Enabling fast poll mode for %s", self.devicename)
        self._fast_poll_enabled = True
        self._fast_poll_count = 0
        self.update_interval = dt.timedelta(seconds=self._fast_poll_interval)
        self._schedule_refresh()
```

Replace `setNormalPollMode()`:
```python
    def setNormalPollMode(self):
        """Return to normal polling, with backoff if there have been failures."""
        self._fast_poll_enabled = False
        if self._consecutive_poll_failures > 0:
            interval = min(
                self._normal_poll_interval * (2 ** self._consecutive_poll_failures),
                self._max_backoff,
            )
            _LOGGER.debug("Setting backoff poll interval %ds for %s (failures: %d)",
                         interval, self.devicename, self._consecutive_poll_failures)
        else:
            interval = self._normal_poll_interval
        self.update_interval = dt.timedelta(seconds=interval)
```

**Step 6: Rewrite `_async_update_data()`**

Replace the entire method with:

```python
    async def _async_update_data(self):
        _LOGGER.debug("Coordinator updating data for %s", self.devicename)

        self._update_poll_counter()

        # Fetch device info (once, on first successful poll)
        if not self._deviceInfoLoaded:
            try:
                async with async_timeout.timeout(30):
                    if await self.read_deviceinfo(disconnect=False):
                        await self._async_update_device_info()
                        self._deviceInfoLoaded = True
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Failed loading device info for %s: %s", self.devicename, err)

        # Fetch config data (once per day)
        if dt.datetime.now().date() != self._last_config_timestamp:
            try:
                async with async_timeout.timeout(30):
                    if await self.read_configdata(disconnect=False):
                        self._last_config_timestamp = dt.datetime.now().date()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Failed loading config for %s: %s", self.devicename, err)

        # Fetch sensor data (every poll)
        try:
            async with async_timeout.timeout(20):
                success = await self.read_sensordata(disconnect=not self._fast_poll_enabled)
                if success:
                    if self._consecutive_poll_failures > 0:
                        _LOGGER.info("Successful read from %s, resetting backoff", self.devicename)
                        self._consecutive_poll_failures = 0
                        self.setNormalPollMode()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("Failed fetching sensor data for %s: %s", self.devicename, err)

        # If we reach here, sensor read failed — increase backoff
        self._consecutive_poll_failures += 1
        _LOGGER.debug("Poll failure %d for %s", self._consecutive_poll_failures, self.devicename)
        self.setNormalPollMode()
```

Key differences from current code:
- No "skip update" early return — always attempts connection
- Only sensor data failure increments the counter (device info and config failures are non-critical)
- Reduced timeouts: 30/30/20 (vs 45/45/30)
- Counter resets on successful sensor read
- Backoff is applied via `setNormalPollMode()` after failure

**Step 7: Run hassfest validation**

```bash
docker run --rm -v $(pwd)/custom_components:/github/workspace/custom_components ghcr.io/home-assistant/hassfest:latest
```

**Step 8: Commit**

```bash
git add custom_components/pax_ble/coordinator.py
git commit -m "fix: remove broken failure cap, restore always-retry polling

- Remove _connection_failures counter that permanently disabled polling
  after 5 failures with no reset path (fixes #87)
- Remove dead _background_reconnect() and _on_device_disconnect()
  (dead since PR #95 removed disconnect callback invocation)
- Simplify _safe_connect() to 3 attempts with 1/2s backoff
- Add adaptive poll interval backoff: slows retries on failure but
  never stops trying. Resets on any successful sensor read.
- Reduce section timeouts to 30/30/20s (from 45/45/30s)
- Restore _schedule_refresh() in setFastPollMode() for immediate
  feedback after writes (was in v1.1.17, removed in later PRs)"
```

---

### Task 3: Clean Up Coordinator Implementations

**Files:**
- Modify: `custom_components/pax_ble/coordinator_calima.py`
- Modify: `custom_components/pax_ble/coordinator_svensa.py`

Remove the dead disconnect callback setup from both coordinator constructors.

**Step 1: Remove callback setup from `CalimaCoordinator.__init__`**

Remove these lines from `coordinator_calima.py` (lines 27-28):
```python
        # Set up disconnect callback
        self._fan.set_disconnect_callback(self._on_device_disconnect)
```

**Step 2: Remove callback setup from `SvensaCoordinator.__init__`**

Remove these lines from `coordinator_svensa.py` (lines 27-28):
```python
        # Set up disconnect callback
        self._fan.set_disconnect_callback(self._on_device_disconnect)
```

**Step 3: Run hassfest validation**

```bash
docker run --rm -v $(pwd)/custom_components:/github/workspace/custom_components ghcr.io/home-assistant/hassfest:latest
```

**Step 4: Commit**

```bash
git add custom_components/pax_ble/coordinator_calima.py custom_components/pax_ble/coordinator_svensa.py
git commit -m "fix: remove dead disconnect callback setup from coordinators

Callback was never invoked since PR #95 removed the invocation from
_handle_disconnect. The _on_device_disconnect method it referenced
has been removed from BaseCoordinator in the previous commit."
```

---

### Task 4: Final Review Pass and CHANGES.md

**Files:**
- Create: `CHANGES.md` (project root)

**Step 1: Full review of modified files**

Read all three modified files end-to-end:
- `custom_components/pax_ble/devices/base_device.py`
- `custom_components/pax_ble/coordinator.py`
- `custom_components/pax_ble/coordinator_calima.py`
- `custom_components/pax_ble/coordinator_svensa.py`

Check for:
- No references to removed methods (`set_disconnect_callback`, `_on_device_disconnect`, `_connection_failures`, `_background_reconnect`, `_reconnection_task`)
- All `asyncio.CancelledError` blocks re-raise properly
- `_connect_lock` used in both `connect()` and `disconnect()`
- `_consecutive_poll_failures` incremented and reset correctly
- `validate_connection()` still works (reads `_client` which is set/cleared under lock)

**Step 2: Run hassfest validation one final time**

```bash
docker run --rm -v $(pwd)/custom_components:/github/workspace/custom_components ghcr.io/home-assistant/hassfest:latest
```

**Step 3: Create CHANGES.md**

```markdown
# Changes: BLE Stability Overhaul

Fixes: #101 (connection hangs), #87 (permanent "skipping update"), PR #99 audit

## `devices/base_device.py`

| Change | What was wrong | What changed |
|--------|---------------|-------------|
| Hard outer timeout on `connect()` | `establish_connection()` could hang indefinitely if BLE handshake stalled, holding the adapter slot forever | Wrapped in `asyncio.wait_for(timeout)` — forcefully cancels after timeout |
| Lock-protected `disconnect()` | `disconnect()` ran without `_connect_lock`, racing with `connect()` — could orphan connections or null the client mid-connect | Now acquires `_connect_lock` before touching `_client` |
| 5s timeout on `disconnect()` | `_client.disconnect()` could hang if BLE stack was wedged | Wrapped in `asyncio.wait_for(timeout=5.0)` |
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
```

**Step 4: Commit CHANGES.md**

```bash
git add CHANGES.md
git commit -m "docs: add CHANGES.md summarizing BLE stability overhaul"
```

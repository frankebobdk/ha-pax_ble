# ha-pax_ble Developer Guide

Home Assistant custom integration for controlling Pax ventilation fans (Calima, Svara, Levante, Svensa) via BLE.

## Validation

The only CI checks — no unit tests, linting, or type-checking exist:

```bash
# hassfest — validates manifest.json, strings.json, services, integration structure
docker run --rm -v $(pwd)/custom_components:/github/workspace/custom_components \
  ghcr.io/home-assistant/hassfest:latest

# HACS — validates repository structure for HACS compatibility
# Runs via hacs/action@main in CI with category: "integration"
```

Workflows: `.github/workflows/hassfest.yaml` and `.github/workflows/validate.yaml`. Both trigger on push, PR, and daily cron.

## Architecture

Four layers, bottom to top:

```
Layer 1: Devices      (devices/*.py)           — BLE GATT reads/writes, struct parsing
Layer 2: Coordinators (coordinator*.py)        — Polling, fast mode, backoff, state dict
Layer 3: Entities     (sensor/number/switch/select/time.py) — HA entity platforms
Layer 4: Setup        (__init__.py)            — Entry lifecycle, device staggering, services
```

### Layer 1: Devices

| Class | File | Models |
|-------|------|--------|
| `BaseDevice` | `devices/base_device.py` | All — shared connection, auth, generic GATT ops |
| `Calima` | `devices/calima.py` | Calima, Svara, Levante |
| `Svensa` | `devices/svensa.py` | Svensa |

Data parsing uses `collections.namedtuple` + `struct.unpack`. Each characteristic has a defined struct format (e.g., `<HHH` for fan speeds, `<4HBHB` for Calima sensor data, `<2B4H5B` for Svensa sensor data).

Characteristic UUID constants live in `devices/characteristics.py` as plain strings.

### Layer 2: Coordinators

| Class | File | Models |
|-------|------|--------|
| `BaseCoordinator` (ABC) | `coordinator.py` | All — abstract base |
| `CalimaCoordinator` | `coordinator_calima.py` | Calima, Svara, Levante |
| `SvensaCoordinator` | `coordinator_svensa.py` | Svensa |

Three abstract methods each coordinator implements:
- `read_sensordata(disconnect=False) -> bool`
- `write_data(key) -> bool`
- `read_configdata(disconnect=False) -> bool`

### Layer 3: Entities

All entity platforms follow the same pattern:
1. Define `PaxEntity` namedtuples describing each entity
2. Define `ENTITIES` (common), `CALIMA_ENTITIES`, `SVENSA_ENTITIES` lists
3. In `async_setup_entry`, match on `coordinator._model` to instantiate the right set

Base class: `PaxCalimaEntity(CoordinatorEntity)` in `entity.py`.

### Layer 4: Setup & Factory

`__init__.py` iterates devices with 10s stagger, calls `getCoordinator()` factory (`helpers.py`), attempts initial refresh with 30s timeout.

Factory uses `match/case` on `DeviceModel` enum (`const.py`):

```python
class DeviceModel(str, Enum):
    CALIMA = "Calima"
    LEVANTE = "Levante"
    SVARA = "Svara"
    SVENSA = "Svensa"
```

## BLE Connection Management

- Uses `bleak-retry-connector` (`establish_connection`, `BleakClientWithServiceCache`)
- `asyncio.Lock` prevents concurrent connections to the same device
- Connection params: `max_attempts=5`, `retry_interval=1.0`, timeout 30s (normal) / 45s (after >2 failures)
- `validate_connection()` reads DEVICE_NAME characteristic as a health check (5s timeout)
- All GATT ops wrapped in `_with_disconnect_on_error()` — auto-disconnects on exception
- Lazy reconnection: disconnect callback sets `_client = None`, reconnects on next poll

### Backoff & Failure Handling

- `_connection_failures` counter, max 5 — skips updates entirely at max
- Exponential backoff: `fast_poll_interval * 2^(failures-1)`, capped at 300s
- Background reconnect task with backoff
- Failures reset to 0 on any successful data read

## Key Conventions

### Polling

| Setting | Default | Range |
|---------|---------|-------|
| Normal interval | 300s (5 min) | 5–999 |
| Fast interval | 5s | 5–999 |

### Fast Mode

Activated after any successful `write_data()`. Polls at fast interval for 10 cycles, then reverts. During fast mode, connections stay open (no disconnect between reads).

### Poll Cycle Order

1. Device info (once, on first poll) — manufacturer, model, revisions
2. Config data (once per day, based on date change)
3. Sensor data (every poll) — disconnects after read unless in fast mode

### Multi-Device Staggering

First device starts immediately; each subsequent device waits 10s before setup.

### Bluetooth Discovery

Only Calima and Levante are auto-discovered via `manifest.json` `bluetooth` entries. Svara and Svensa require manual addition.

## Adding a New Device Model

1. **`const.py`** — Add to `DeviceModel` enum
2. **`devices/new_model.py`** — Subclass `BaseDevice`, define characteristic UUIDs in `self.chars`, implement `namedtuple` + `struct.unpack` parsing, write getter/setter methods
3. **`devices/characteristics.py`** — Add any new characteristic key constants
4. **`coordinator_new_model.py`** — Subclass `BaseCoordinator`, implement `read_sensordata`, `write_data`, `read_configdata`
5. **`helpers.py`** — Add `case` to `getCoordinator()` factory
6. **Entity platforms** (`sensor.py`, `number.py`, `switch.py`, `select.py`, `time.py`) — Add `NEW_MODEL_ENTITIES` list and `case` branch in `async_setup_entry`
7. **`manifest.json`** — (Optional) Add `local_name` for auto-discovery

Calima/Svara/Levante share one device driver and coordinator (identical BLE protocol). Svensa has entirely different UUIDs (`7c4adc0x-...` family), struct layouts, and a special pairing mechanism (write PIN 0, read back real PIN). Whether a new model needs its own driver depends on BLE protocol compatibility.

## Workflow Orchestration

### 1. Plan Mode Default

- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy

- Offload research, exploration, and parallel analysis to subagents to keep main context window clean
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop

- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done

- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)

- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing

- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests → then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.
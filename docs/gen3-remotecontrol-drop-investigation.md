# Brief: Gen3 remote-control power drops — review request

## Resolution (2026-07-04)

**Root cause was not a bug in this integration's code.** The periodic revert-to-self-consumption
(power dropping to ~0 W for a few seconds roughly once a minute, with the inverter's front-panel
status flipping `Normal(Remote)` → `Normal` → `Normal(Remote)` in sync) was caused by **external
Modbus RS485 bus contention from a separate, independent interface** — a "jbox" device connected
directly to the inverter via Modbus RTU over a USB adapter, running `mbusd` and a gateway
container, polling and occasionally writing to the same inverter concurrently with solax_modbus's
own connection (via the inverter's built-in Ethernet/Modbus TCP port) and the always-on (but
command-idle) SolaX WiFi/LAN dongle. Stopping the jbox's `mbusd` and gateway containers
immediately and reliably stopped the dropouts.

How this was confirmed, in order:

1. A from-scratch reference client (`se-vpp-client/vpp-local`) driving the same inverter over the
   same Modbus TCP path stayed perfectly steady for extended periods, ruling out the transport
   (TCP vs. RTU) and the inverter's own network module as inherently causing this for every client.
2. Direct observation of the inverter's own front-panel display (bypassing all HA/software
   sampling) confirmed the drop is a genuine, brief revert of the remote-control session itself —
   not a sensor/graph sampling artifact (HA's own sensors frequently missed it due to their poll
   cadence relative to how brief the dip is).
3. The behavior was shown to be **intermittent on a timescale of tens of minutes** (drops present
   for ~8 minutes, then completely absent — confirmed via direct panel observation, not just
   sensor sampling — for 40+ minutes, then resumed) with **zero change to solax_modbus's config or
   code** across those boundaries. A deterministic bug in this integration's write path would not
   spontaneously stop and restart with nothing on our side changing; this was the strongest signal
   that the cause was external.
4. A raw `pymodbus` wire-level trace (`pymodbus.logging` at debug) captured during an active
   dropout showed a **second, independent Modbus session** — a completely separate, steadily
   incrementing transaction-ID sequence (e.g. `0x5ad1, 0x5ad2, 0x5ad3...`, roughly every 3 seconds,
   reading what look like energy-accumulator registers) with response frames appearing directly on
   solax_modbus's own TCP socket with **no corresponding outbound request logged from our own
   client**. This was direct proof of a second master's traffic on the wire.
5. Shutting down the jbox's `mbusd`/gateway containers stopped the dropouts immediately.

During elimination, the following were ruled out as the cause (kept here so this isn't
re-investigated if the symptom ever recurs for a genuinely different reason):

- Register/burst content, write cadence, and byte-level encoding — confirmed identical to the
  known-good `vpp-local` reference client (same register, same FC16 count, same word order).
- `export_duration` (register `0x9F`) value — toggling it had no effect.
- The `lock_state` "Unlocked" / `system_on` unlock sequence that `vpp-local` sends once at
  startup — replicating it manually had no effect.
- solax_modbus's own background polling (fast/medium/slow scan groups) — the drop persisted
  identically with all background reads paused, leaving only the Gen3 RC button + its 2 s
  keepalive active.
- Modbus unit/device ID — confirmed identical (4) on both clients; an apparent mismatch in
  `vpp-local`'s own log was a `pymodbus` PDU-decode display quirk, not a real difference.
- TCP reconnects/errors — no `close`/`connect`/`ModbusException` events anywhere near a drop.
- Write-path locking — `async_write_registers_multi` correctly serializes the actual wire write
  via `self._lock`; no race condition was found or needed to explain the symptom.

**Open question, not resolved by this investigation:** whether the actual failure mechanism was
pure RS485-level bus collision (two independent masters — the inverter's built-in Ethernet
Modbus-TCP↔RTU gateway, and the jbox's own RTU master via its USB adapter — transmitting on the
shared internal bus with no arbitration between them), or whether the jbox's own `mbusd`/gateway
software was itself periodically issuing something (e.g. on a roughly-minute cadence) that
specifically caused the inverter to drop its remote-control session, independent of raw
collision. The user previously ran multiple masters successfully through a single `mbusd`
instance (which serializes all downstream clients into one real bus master) without this
symptom — the failure mode here appeared specifically when solax_modbus talked to the inverter
directly while the jbox's `mbusd`/gateway *also* drove the bus directly and independently, i.e.
two uncoordinated masters rather than one serializing proxy. If this recurs, checking the jbox's
own gateway/mbusd logs for periodic (~60 s) activity coinciding with drops — or routing
solax_modbus through the same `mbusd` instance instead of connecting to the inverter directly —
would be the next things to check.

No code changes were made to this repository as a result of this investigation. The Gen3
keepalive (`10e3c40b`) and atomic multi-register-write fix (`8ed63c36`) made earlier in this same
session remain valid, unrelated improvements and should be kept.

## Context

I (the user) added Gen3 X1 AC remote power control to this integration myself
(with AI assistance) — see `autorepeat_function_remotecontrol_recompute_gen3`
in `plugin_solax.py` and its shared helper `_compute_remotecontrol_ap_target`,
plus the associated button/number/select entities (commits `8a9cc37`,
`23496bb`, `b8b758d`, `44d94e7`).

Since then, a companion Home Assistant integration (`grid_coordinator`, a
separate custom component that drives this inverter's RC mode every 10s to
track an EMHASS optimiser target) has intermittently seen the Solax battery
power drop to ~0 W and recover on its own, at an interval that **looks like
roughly once a minute but has not been rigorously timed** — treat "~60s" as a
weak, unconfirmed observation, not a established period. It could plausibly
just be an artifact of how often a given sensor's displayed state refreshes
rather than a real fixed period in the underlying behavior.

This file is a handoff brief for a fresh session reviewing **this repo**
(the solax_modbus integration itself, focused on the Gen3 RC patch) — it is
not asking about `grid_coordinator`'s own code, which has already been
extensively audited (see "Ruled out" below).

## What's been ruled out (please don't re-litigate these)

1. **Solax-modbus's own poll/scan-interval cadence.** Tested at both very fast
   (~2s) and very slow (~180s) settings for the "fast" scan group (the one
   carrying `inverter_power`, `battery_power_charge`, `measured_power`,
   `battery_capacity`, etc.). The physical inverter's own front-panel display
   was watched directly (bypassing all HA sensor state) at both extremes —
   the drop-to-zero happened identically regardless of polling speed. The
   earlier appearance of "it's fixed" at slow polling was confirmed to be a
   stale-sensor-display artifact in HA (sensors just weren't refreshing often
   enough to show the real drops), not a genuine fix.
2. **grid_coordinator's own write cadence/pattern.** Tested four different
   write strategies from the companion integration: (a) writing
   `remotecontrol_active_power` unconditionally every tick, (b) writing it
   only on real setpoint changes (deadband-gated), (c) writing the
   `export_duration` select unconditionally every tick, (d) writing it only
   once per RC-session activation. The drop recurred identically across all
   four combinations — none of grid_coordinator's write patterns caused or
   prevented it.
3. **The `export_duration` register (0x9F) not being extended.** grid_coordinator
   had a genuine, unrelated bug where its configured entity ID for this
   select (`select.solax_export_duration`) didn't match the real entity
   (`select.solax_modbus_export_duration`), so that write was a silent no-op
   (confirmed via a recurring `Referenced entities ... missing` warning in the
   HA log) for a long stretch of testing. That's now fixed on the
   grid_coordinator side, but is unrelated to the periodic drop — the drop
   still recurred both before and after that fix.

## What's already been found and fixed (elsewhere, for context only — not your task)

grid_coordinator had a real bug of its own: after an EMHASS plan
regeneration pulled it out of self-consumption mode, a Modbus write from
grid_coordinator (to `remotecontrol_power_control` or `export_duration`)
hung for ~5s and then failed with:

```
Modbus Error: [Input/Output] Request cancelled outside pymodbus.
```

Because that failure happened before grid_coordinator reached its
`remotecontrol_active_power` write in the same tick, the real setpoint never
got sent — but grid_coordinator's code still pressed the RC trigger button
unconditionally afterward, which re-sent *this integration's own cached*
`remotecontrol_active_power` value (still 0 W, left over from the prior idle
period) rather than anything fresh. That's now fixed on the grid_coordinator
side (it skips the trigger press when its own setpoint write fails).

That event is relevant to you for one reason: **the error message
`"Request cancelled outside pymodbus"` originates from somewhere in this
integration's Modbus write path, not from grid_coordinator itself** — it's
worth understanding what can produce that specific error, since it implies
something *outside* pymodbus's own request lifecycle interrupted an in-flight
write (e.g. a concurrent reconnect/`close()` on the same client racing an
in-flight `write_registers()` call from a different task).

## Facts established about this codebase (verified this session, may still be worth double-checking)

- `autorepeat_function_remotecontrol_recompute_gen3` builds a 5-register FC16
  burst at `0x7C`: `[enable_flag(U16), active_power(S32), reactive_power(S32)=0]`.
  Unlike the non-Gen3 `autorepeat_function_remotecontrol_recompute`, it does
  **not** include `remotecontrol_duration`/`remotecontrol_timeout` fields in
  its write — whatever governs the inverter's own command-expiry window for
  Gen3 is entirely the separate `export_duration` register (`0x9F`), not part
  of this burst.
- Both recompute functions share `_compute_remotecontrol_ap_target`, which
  recomputes the power target from **live** `house_load`/`measured_power`/
  phase-current data every time it runs — including phase-envelope
  protection and `import_limit - house_load` bounds clamping that can reduce
  `ap_target`, in principle down to 0, independent of what any caller
  requested.
- This recompute isn't only triggered by an explicit button press —
  `SolaXModbusButton.async_press()` does write immediately when pressed, but
  it also sets `_repeatUntil[key] = now + remotecontrol_autorepeat_duration - 0.5`,
  and the "execute autorepeat entities" loop in `__init__.py`
  (`async_read_modbus_data`, ~line 1611) **re-evaluates and re-writes the
  same payload from scratch on every device-group poll cycle** until that
  deadline lapses — i.e. this recompute runs far more often than just once
  per external button press, on whatever cadence the relevant scan group
  polls at.
- Register `0x9F` (`export_duration`, the SELECT entity written to extend the
  command-expiry timer) is **never included in any of this plugin's polled
  register blocks** — it's write-only from HA's perspective. There's a
  separate **read-only** mirror sensor at register `0x10B` (a
  `SolaXModbusSensorEntityDescription`, `internal=True`) decoding the same
  value mapping (`4: "Default"`, `900: "15 Minutes"`, etc.) from a
  *different* physical address. This means the SELECT entity's displayed
  state is purely an echo of the last value written from HA — it is never
  verified against a real hardware read-back. Worth checking whether the
  actual register `0x9F` genuinely holds whatever was last written, or
  whether it silently reverts on the inverter side (possible only via the
  `0x10B` mirror, which does get read on this plugin's slow/default scan
  group).
- `async_write_registers_multi` (`__init__.py`, ~line 1258) builds
  `regs_out` by iterating payload tuples and calling `convert_to_registers()`
  per tuple, with a **per-tuple** try/except: if encoding of any single
  tuple raises, that tuple's registers are silently skipped (logged as an
  error) but the loop continues, and the resulting **shorter, misaligned**
  `regs_out` is still written via a single `write_registers()` call. For a
  burst meant to be atomic (like the Gen3 5-register command), this seems
  like a real structural bug — a partial/misaligned write could plausibly
  produce genuinely invalid data on the wire (worth checking whether this can
  actually be triggered for the values this burst carries in practice, and
  whether it should instead validate/encode the whole payload first and abort
  the entire write if any tuple fails).

## Also worth knowing

- The inverter was observed to **fully reboot once** during testing (not
  just an RC-mode revert-to-self-consumption). Timing was uncertain, roughly
  in the 2026-07-03 14:50–15:00 window. We could not conclusively tie the
  reboot to a specific logged event; the closest candidate is the
  `"Request cancelled outside pymodbus"` failure above (~14:50:02–07), but
  that isn't proven — treat the reboot as a serious open question, not
  something already explained.
- Solax-modbus's config-entry was seen reloading multiple times during the
  same test session (`trying to load plugin` / `async_setup_entry called`)
  — these coincided with the user manually changing scan-interval options
  in the integration's config UI, which is expected/benign on its own, but
  reloading the config entry while an RC session is actively being commanded
  is inherently a bit of a stress case (entities torn down and rebuilt,
  Modbus client potentially reconnected) — worth considering whether a reload
  mid-command could produce a bad write, distinct from the steady-state
  drop.

## What we're asking you to look for

Focused on **this integration's code**, not polling configuration and not
grid_coordinator:

1. Any place — in the Gen3 patch or in pre-existing code — that could write
   an unintended/zero/wrong value to the power-control registers
   (`remotecontrol_power_control`, `remotecontrol_active_power`,
   register `0x7C`–`0x80`) on some periodic or event-driven basis unrelated
   to what the calling entity (button/number/select) was actually asked to
   do.
2. Any interaction between the **newly added** Gen3 registers and the
   **pre-existing** export-related registers/entities that were already part
   of this integration before the Gen3 patch — e.g. `export_duration`
   (`0x9F`), its read-only mirror (`0x10B`), `export_control_user_limit`,
   `config_export_control_limit_readscale`, `config_max_export` — anything
   that shares a register block, an entity dependency chain, or a periodic
   self-heal/rewrite path with the Gen3 power-control registers.
3. The `async_write_registers_multi` per-tuple silent-failure behavior
   described above — confirm whether it can actually produce a malformed
   write for realistic Gen3 payload values, and whether it should be made
   all-or-nothing.
4. Anything that could produce `"Request cancelled outside pymodbus"` —
   overlapping read/write tasks, lock contention, or a reconnect racing an
   in-flight write.

Please don't spend time on: solax-modbus scan-interval tuning, or
grid_coordinator's write cadence — both have been empirically ruled out as
explanations for the drop (see above).

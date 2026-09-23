# Hubitat driver and companion-app development

Load this reference only for Hubitat code development or debugging. For ordinary device control, use the helper workflow in the main skill.

## Architecture

- Drivers manage virtual state and emit events.
- Companion apps subscribe to those events and send physical IR/RF commands.
- A driver and companion app must not both send the physical command; that causes duplicate actions.
- Companion apps should call `unsubscribe()` in `updated()` before reinitializing subscriptions.
- Event-handler ordering is not guaranteed. Prefer one subscribed event type and one transition handler rather than coordinating several handlers with flags.

## Cross-device access

- `capability.*` selectors work in apps, not reliably in drivers.
- In a driver, accept a text device ID and resolve it with `getDeviceAt(id.toInteger())`.
- Do not rely on `location.devices`, localhost HTTP calls, blocked DeviceWrapper classes, `getClass()`, or `metaClass` in the driver sandbox.

## Alexa virtual-device events

For Alexa-controlled virtual devices, send state events with `isStateChange: true`. For production-quality events, also provide `type: "digital"` and `descriptionText`.

Hubitat Echo Skill's fan ModeController path has a known state-reporting problem. The most reliable workaround for complex IR functions is one momentary virtual switch per function, mapped by a companion app and exposed to Alexa routines. See `alexa-capabilities.md`, `ir-blast-companion-pattern.md`, and `small-fan-ir-controller.md`.

## Scheduling pitfall

Repeated `runIn()` calls using the same handler overwrite each other. Use recursive scheduling with state tracking for repeated delayed actions.

## Internal API

Development operations use the hub's internal endpoints, including:

- `/driver/list/data`
- `/driver/list/single/data/{id}`
- `/driver/saveOrUpdateJson`
- `/app/saveOrUpdateJson`
- `/device/fullJson/{id}`
- `/device/eventsJson/{id}`

When updating a driver, fetch and submit the current version. Hubitat increments it automatically. Verify the saved source and the live device afterward. Never expose credentials or lock codes.

## Release workflow

Before publishing driver/app code:

1. Update version and release notes.
2. Remove development suffixes from release filenames.
3. Update README references.
4. Keep agent coordination files ignored.
5. Run a PII and credential sweep.
6. Commit/push only after verification.
7. Deploy to live Hubitat only after Angela authorizes the change, then verify the device.

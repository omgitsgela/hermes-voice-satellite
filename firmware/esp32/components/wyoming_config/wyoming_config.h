#ifndef WYOMING_CONFIG_H
#define WYOMING_CONFIG_H
#include <stddef.h>
#include <stdint.h>
#include "cJSON.h"

/* Health of the last apply attempt, exactly the vocabulary the bridge's config-plane
 * report() accepts: unknown | healthy | degraded | error. */
enum { WY_CFG_HEALTH_UNKNOWN = 0, WY_CFG_HEALTH_HEALTHY = 1,
       WY_CFG_HEALTH_DEGRADED = 2, WY_CFG_HEALTH_ERROR = 3 };

/* The device half of the declarative configuration plane (docs/configuration-plane.md),
 * for the XVF3800 satellite. A pure state machine: it owns the schema allowlist, the
 * generation guard, the desired/actual bookkeeping, durable persistence, and the report.
 * It does no transport I/O of its own -- the session core decodes the inbound
 * hermes.config.desired frame and passes its `data` JSON here, then sends the returned
 * hermes.config.reported `data` JSON. The two seams that touch the outside world --
 * durable storage and hardware apply -- are injected callbacks, so this exact source
 * compiles on the host (no ESP/RTOS/socket/NVS) and is tested directly, the way the
 * session core is.
 *
 * This is the DEVICE half, so it encodes this device's apply model (the reference
 * pi/config_agent.py leaves that to the injected adapter, which returns `actual`; here
 * the agent owns `actual` because it is device-specific and the C adapter only does the
 * side-effects and reports health). The model:
 *   - playback_volume : applied immediately (the session's live volume the pump already
 *                       uses); always in `actual` when present in the settings.
 *   - wake_word, wake_threshold : staged for the wake detector (the next increment);
 *                       NOT in `actual` yet (nothing to apply to), remembered as pending.
 *   - mic_gain, led_brightness : the XMOS codec / APA102 strip -- no adapter installed
 *                       yet; not in `actual`, contribute `degraded`.
 *   - locale, update_channel : not device-applied (bridge: next_turn / next_update_check);
 *                       recorded, not in `actual`. */
typedef struct wy_config wy_config;
size_t wy_config_size(void);
int wy_config_init(wy_config *c, const char *device_id);

/* Injected durable storage. On the device it is the NVS namespace; on the host an
 * in-memory buffer. load returns 0 with *outlen set when a persisted blob is available, or
 * nonzero when there is none (first boot). save returns 0 only once the state is durable:
 * a report is produced only after the state is persisted -- acceptance follows durability. */
typedef int (*wy_cfg_save_fn)(void *user, const char *json, size_t len);
typedef int (*wy_cfg_load_fn)(void *user, char *out, size_t cap, size_t *outlen);
void wy_config_set_storage(wy_config *c, wy_cfg_save_fn save, wy_cfg_load_fn load, void *user);

/* Injected apply adapter (the device glue). The agent serializes the validated settings to a
 * JSON string and hands it over; the adapter performs the side-effects it can (set the live
 * volume, stage the wake settings) and writes the ACTUAL state it really reached -- a JSON
 * object string of a subset of the settings -- into out_actual_json (actual_cap-bounded,
 * NUL-terminated), the resulting health through *out_health, and an optional error through
 * out_error (NUL-terminated, err_cap-bounded; leave it empty for a clean result). Returning
 * anything but WY_OK, or leaving *out_health unset, is treated as an apply failure
 * (health=error, actual empty). The adapter is the source of truth for `actual`: the agent
 * never claims a setting applied that the adapter did not report, so a device with no adapter
 * installed degrades to an empty actual -- nothing is claimed hardware-applied that was not. */
typedef int (*wy_cfg_apply_fn)(void *user, const char *settings_json,
                               char *out_actual_json, size_t actual_cap,
                               int *out_health, char *out_error, size_t err_cap);
void wy_config_set_apply(wy_config *c, wy_cfg_apply_fn apply, void *user);
void wy_config_destroy(wy_config *c);

/* Process one decoded hermes.config.desired `data` object (as a NUL-terminated JSON string)
 * and build the hermes.config.reported `data` JSON into report_out (NUL-terminated).
 * Always returns WY_OK once a report is produced; the report's health/error carries the
 * outcome -- a stale, rejected, or replayed desired re-reports the device's CURRENT state
 * rather than the rejected desired. A NULL desired or a NULL/short report buffer is
 * WY_INVALID. */
int wy_config_step(wy_config *c, const char *desired_json, char *report_out, size_t report_cap);
#endif

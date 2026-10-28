/**
 * @file
 * Internal, optional public-display renderer; never an input-method transport.
 * Geometry is logical compositor space, image dimensions are pixels, and
 * fractional scales are in 1/120 units. No handle is thread-safe.
 */
#ifndef INKLOW_WAYLAND_GHOST_H
#define INKLOW_WAYLAND_GHOST_H

#include "wayland_client.h"

/* Private helper: owned exclusively by inklow_wl on the Qt event-loop thread.
 * Notifications carry identity only, never application strings or pixels.
 */
struct inklow_ghost;
/** Synchronous identity notification; the callback must not destroy its owner. */
typedef void (*inklow_ghost_notify)(void *, int type, uint32_t state,
                                    uint64_t scene, uint64_t frame);
/**
 * Connect to explicit WAYLAND_DISPLAY and require its peer PID to match the
 * private compositor. Performs startup roundtrips; returns an owned handle or
 * NULL if authentication, required protocols or allocation fail.
 * Callback/userdata are borrowed until close; callbacks begin after setup.
 */
struct inklow_ghost *inklow_ghost_connect(uint32_t compositor_pid, inklow_ghost_notify, void *);
/** Borrow the display fd for readiness watches, or -1 when unavailable. */
int inklow_ghost_fd(struct inklow_ghost *);
/** Process available events; 1 flushed, 0 write readiness needed, -1 unavailable. */
int inklow_ghost_dispatch(struct inklow_ghost *);
/** Flush queued requests without waiting; same return values as dispatch. */
int inklow_ghost_flush(struct inklow_ghost *);
/** Apply authenticated desktop state: 0 hidden, 1 showing, other values unknown. */
void inklow_ghost_desktop_state(struct inklow_ghost *, int state);
/** Write the unique active PID's frame/scene on success (1); otherwise return 0. */
int inklow_ghost_window(struct inklow_ghost *, uint32_t, struct inklow_wl_ghost_window *);
/**
 * Prepare a PID/scene rectangle wholly inside one observed untransformed output.
 * Returns 1 ready, 2 waiting for configure/scale, 0 refused and hidden.
 */
int inklow_ghost_prepare(struct inklow_ghost *, uint32_t, uint64_t, int, int, int, int);
/** Return the surface's preferred scale in 1/120 units, or 0 if unavailable. */
int inklow_ghost_scale(struct inklow_ghost *);
/**
 * Copy borrowed ARGB8888 pixels (width/height in pixels, stride in bytes).
 * scene must be current and nonzero frame must increase within that scene.
 * Dimensions must equal ceil(prepared logical dimension * scale / 120).
 * Returns 1 queued, 2 buffers busy, 0 refused; event 7 alone proves presentation.
 */
int inklow_ghost_present(struct inklow_ghost *, uint64_t, uint64_t,
                         const void *, int, int, int);
/** Revoke feedback, destroy the surface role, and erase pixels when released. */
void inklow_ghost_hide(struct inklow_ghost *);
/** Release all resources without notifications; accepts NULL. */
void inklow_ghost_close(struct inklow_ghost *);

#endif

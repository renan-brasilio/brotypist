#ifndef INKLOW_WAYLAND_CLIENT_H
#define INKLOW_WAYLAND_CLIENT_H

#include <stddef.h>
#include <stdint.h>

#if defined(__GNUC__)
#define INKLOW_WL_API __attribute__((visibility("default")))
#else
#define INKLOW_WL_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct inklow_wl inklow_wl;
struct inklow_wl_event {
    int type;
    uint32_t generation, serial, time, key, state, modifiers;
    int32_t x, y;
    const char *name;
    uint64_t scene, frame;
};
typedef int (*inklow_wl_callback)(void *, const struct inklow_wl_event *);

struct inklow_wl_ghost_window {
    uint64_t scene;
    uint32_t pid;
    int32_t x, y, width, height;
};

/* Single-threaded, same event-loop ownership. Callbacks must not close the client.
 * Key callback names are borrowed until return; no event contains field text.
 * Canonical modifiers: Shift=1, Control=4, Alt=8, Super=64, unsupported=128.
 * Event 1 state: 1 missing initial purpose; 2 known-safe initial state incomplete.
 * Ready and known-sensitive contexts use 0; no field text crosses this event.
 * Event 5 reports a reusable compositor buffer; retry only a pending frame.
 * Event 6: state 1 invalidates scene geometry; state 2 retries configure/buffer
 * readiness without changing scene. Event 7: matching scene/frame state 1 is
 * compositor presentation proof; state 0 revokes/discards the frame.
 */
INKLOW_WL_API inklow_wl *inklow_wl_connect(int owned_fd, inklow_wl_callback, void *userdata);
INKLOW_WL_API int inklow_wl_fd(inklow_wl *);
/* Private socket SO_PEERCRED PID, or 0 if unavailable/disconnected.
 * Optional ghost authentication failure never disables private operations. */
INKLOW_WL_API uint32_t inklow_wl_compositor_pid(inklow_wl *);
/* dispatch/flush: 1 drained, 0 pending writes (EAGAIN), -1 disconnected.
 * Watch writable readiness while pending, then call flush again.
 */
INKLOW_WL_API int inklow_wl_dispatch(inklow_wl *);
INKLOW_WL_API int inklow_wl_flush(inklow_wl *);
INKLOW_WL_API void inklow_wl_close(inklow_wl *);
/* Observed pending/uncertain compose sequences suspend state/matches/commit.
 * Missing positive compose-table proof alone does not disable manual state. */
INKLOW_WL_API int inklow_wl_state(inklow_wl *, uint32_t *generation, uint32_t *serial);
/* Stable context lifetime, independent of text/commit revision invalidation. */
INKLOW_WL_API uint64_t inklow_wl_context(inklow_wl *);
INKLOW_WL_API int inklow_wl_matches(inklow_wl *, uint32_t generation, uint32_t serial,
                                   const char *utf8_text, size_t length, size_t cursor_bytes);
INKLOW_WL_API int inklow_wl_commit(inklow_wl *, uint32_t generation, uint32_t serial,
                                  const char *utf8_text);
/* present: 1 attached, 2 buffers busy (retry on event 5), 0 unavailable. */
INKLOW_WL_API int inklow_wl_present(inklow_wl *, const void *argb32, int width,
                                   int height, int stride, int scale);
INKLOW_WL_API void inklow_wl_hide(inklow_wl *);

/* Optional public-display inactive renderer; absence never affects forwarding.
 * All coordinates are observed logical compositor coordinates. prepare returns
 * 1 ready, 2 awaiting configure/scale, 0 refused; present returns 1 queued,
 * 2 all buffers busy, 0 refused. Queueing is NOT presentation proof.
 */
INKLOW_WL_API int inklow_wl_ghost_fd(inklow_wl *);
INKLOW_WL_API int inklow_wl_ghost_dispatch(inklow_wl *);
/* Absent/disabled optional renderer: fd -1, dispatch -1, flush 1 (no work). */
INKLOW_WL_API int inklow_wl_ghost_flush(inklow_wl *);
/* Authenticated external desktop state: -1 unknown, 0 hidden, 1 showing.
 * Unknown/invalid values deny rendering. Protocol showing remains a separate
 * deny; changes revoke scene/frame authorization synchronously before events. */
INKLOW_WL_API void inklow_wl_ghost_desktop_state(inklow_wl *, int state);
INKLOW_WL_API int inklow_wl_ghost_window(inklow_wl *, uint32_t pid, struct inklow_wl_ghost_window *);
INKLOW_WL_API int inklow_wl_ghost_prepare(inklow_wl *, uint32_t pid, uint64_t scene,
                                        int x, int y, int width, int height);
INKLOW_WL_API int inklow_wl_ghost_scale(inklow_wl *);
INKLOW_WL_API int inklow_wl_ghost_present(inklow_wl *, uint64_t scene, uint64_t frame,
                                        const void *argb32, int pixel_width, int pixel_height, int stride);
INKLOW_WL_API void inklow_wl_ghost_hide(inklow_wl *);
/* Positive safe current-context/XKB compose evidence, never an assumed idle. */
INKLOW_WL_API int inklow_wl_composition_inactive(inklow_wl *);

#ifdef __cplusplus
}
#endif
#endif

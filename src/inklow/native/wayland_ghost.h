#ifndef INKLOW_WAYLAND_GHOST_H
#define INKLOW_WAYLAND_GHOST_H

#include "wayland_client.h"

/* Private helper: owned exclusively by inklow_wl on the Qt event-loop thread.
 * Notifications carry identity only, never application strings or pixels.
 */
struct inklow_ghost;
typedef void (*inklow_ghost_notify)(void *, int type, uint32_t state,
                                    uint64_t scene, uint64_t frame);
struct inklow_ghost *inklow_ghost_connect(uint32_t compositor_pid, inklow_ghost_notify, void *);
int inklow_ghost_fd(struct inklow_ghost *);
int inklow_ghost_dispatch(struct inklow_ghost *);
int inklow_ghost_flush(struct inklow_ghost *);
void inklow_ghost_desktop_state(struct inklow_ghost *, int state);
int inklow_ghost_window(struct inklow_ghost *, uint32_t, struct inklow_wl_ghost_window *);
int inklow_ghost_prepare(struct inklow_ghost *, uint32_t, uint64_t, int, int, int, int);
int inklow_ghost_scale(struct inklow_ghost *);
int inklow_ghost_present(struct inklow_ghost *, uint64_t, uint64_t,
                         const void *, int, int, int);
void inklow_ghost_hide(struct inklow_ghost *);
void inklow_ghost_close(struct inklow_ghost *);

#endif

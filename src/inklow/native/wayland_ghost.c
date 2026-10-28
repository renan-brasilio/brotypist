/*
 * Optional click-through Wayland ghost renderer on an authenticated public display.
 * Plasma frame observations and output metadata authorize a scene; configure,
 * fractional scale and presentation feedback separately fence each paint.
 * The owning private input-method client remains usable when this helper fails.
 * All state and callbacks stay on its event-loop thread; titles are discarded.
 */
#define _GNU_SOURCE
#include "wayland_ghost.h"
#include "wlr-layer-shell-client-protocol.h"
#include "plasma-window-management-client-protocol.h"
#include "xdg-output-client-protocol.h"
#include "fractional-scale-client-protocol.h"
#include "viewporter-client-protocol.h"
#include "presentation-time-client-protocol.h"

#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>
#include <wayland-client.h>

#define GHOST_MAX_OUTPUTS 32u
#define GHOST_MAX_WINDOWS 512u
#define GHOST_MAX_IMAGE (64u * 1024u * 1024u)
#define GHOST_MAX_DIMENSION 16384
#define GHOST_MAX_SCALE (16u * 120u)

/* A complete logical-output batch is required before geometry can authorize paint. */
struct ghost_output {
    struct ghost_output *next;
    struct inklow_ghost *owner;
    struct wl_output *proxy;
    struct zxdg_output_v1 *logical;
    uint32_t id;
    int32_t x, y, width, height, transform, integer_scale;
    bool position, size, geometry, scale, done;
};

struct ghost_window {
    struct ghost_window *next;
    struct inklow_ghost *owner;
    struct org_kde_plasma_window *proxy;
    uint32_t pid, flags;
    int32_t x, y, width, height;
    bool initial, geometry, state, mapped;
};

/* busy belongs to compositor release; dirty/erase track deferred pixel retirement. */
struct ghost_buffer {
    struct inklow_ghost *owner;
    struct wl_buffer *proxy;
    void *pixels;
    size_t size;
    int width, height, stride;
    bool busy, erase, dirty;
};

struct inklow_ghost {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct zwlr_layer_shell_v1 *layers;
    struct org_kde_plasma_window_management *windows;
    struct zxdg_output_manager_v1 *outputs;
    struct wp_viewporter *viewporter;
    struct wp_fractional_scale_manager_v1 *fractional;
    struct wp_presentation *presentation;
    struct ghost_output *output_list, *target_output;
    struct ghost_window *window_list;
    struct wl_surface *surface;
    struct zwlr_layer_surface_v1 *layer;
    struct wp_viewport *viewport;
    struct wp_fractional_scale_v1 *surface_scale;
    struct wp_presentation_feedback *feedback;
    struct ghost_buffer buffers[2];
    inklow_ghost_notify notify;
    void *userdata;
    uint64_t scene, frame, frame_scene, last_frame;
    uint32_t compositor_id, shm_id, layers_id, windows_id, outputs_id;
    uint32_t viewporter_id, fractional_id, presentation_id, target_pid, scale;
    unsigned output_count, window_count;
    int x, y, width, height;
    bool configured, visible, failed, hiding, desktop, desktop_known, feedback_output;
    bool external_desktop_known, external_desktop;
};

static void erase(void *data, size_t length)
{
    volatile unsigned char *p = data;
    while (length--) *p++ = 0;
}

static void notify(struct inklow_ghost *g, int type, uint32_t state,
                   uint64_t scene, uint64_t frame)
{
    if (g->notify) g->notify(g->userdata, type, state, scene, frame);
}

/* Clear frame identity before notification so reentrant hides cannot revoke twice. */
static void revoke_frame(struct inklow_ghost *g)
{
    if (g->feedback) {
        wp_presentation_feedback_destroy(g->feedback);
        g->feedback = NULL;
    }
    uint64_t frame = g->frame, scene = g->frame_scene;
    g->frame = g->frame_scene = 0;
    g->feedback_output = false;
    if (frame) notify(g, 7, 0, scene, frame);
}

static void destroy_surface(struct inklow_ghost *g)
{
    if (g->surface_scale) wp_fractional_scale_v1_destroy(g->surface_scale);
    if (g->viewport) wp_viewport_destroy(g->viewport);
    if (g->layer) zwlr_layer_surface_v1_destroy(g->layer);
    if (g->surface) wl_surface_destroy(g->surface);
    g->surface_scale = NULL;
    g->viewport = NULL;
    g->layer = NULL;
    g->surface = NULL;
    g->target_output = NULL;
    g->target_pid = g->scale = 0;
    g->configured = g->visible = false;
}

void inklow_ghost_hide(struct inklow_ghost *g)
{
    if (!g || g->hiding) return;
    g->hiding = true;
    /* Clear local authorization before callbacks can attempt another paint. */
    g->configured = false;
    if (g->surface && g->visible) {
        wl_surface_attach(g->surface, NULL, 0, 0);
        wl_surface_commit(g->surface);
    }
    g->visible = false;
    revoke_frame(g);
    /* A new role/surface for every remap enforces the initial configure fence
     * even on compositors that coalesce an unmap and the next empty commit.
     */
    destroy_surface(g);
    for (size_t i = 0; i < 2; ++i) {
        struct ghost_buffer *b = &g->buffers[i];
        b->erase = true;
        if (!b->busy && b->pixels && b->dirty) {
            erase(b->pixels, b->size);
            b->dirty = b->erase = false;
        }
    }
    if (g->display && !g->failed) inklow_ghost_flush(g);
    g->hiding = false;
}

/* Scene changes retire all geometry-dependent work, including presented frames. */
static void scene_changed(struct inklow_ghost *g)
{
    if (++g->scene == 0) ++g->scene;
    g->last_frame = 0;
    inklow_ghost_hide(g);
    notify(g, 6, 1, g->scene, 0);
}

void inklow_ghost_desktop_state(struct inklow_ghost *g, int state)
{
    if (!g || g->failed) return;
    bool known = state == 0 || state == 1;
    bool showing = state == 1;
    if (g->external_desktop_known == known && g->external_desktop == showing) return;
    /* Publish the guard first; scene_changed invalidates scene and frame
     * before callbacks, including synchronous attempts to repaint. */
    g->external_desktop_known = known;
    g->external_desktop = showing;
    scene_changed(g);
}

static void failed(struct inklow_ghost *g)
{
    if (g->failed) return;
    g->failed = true;
    scene_changed(g);
    /* Registry removal may leave a healthy transport. Send the unmap/role
     * teardown even though the optional renderer is now permanently disabled. */
    if (g->display) wl_display_flush(g->display);
}

int inklow_ghost_flush(struct inklow_ghost *g)
{
    if (!g || !g->display || g->failed) return -1;
    if (wl_display_flush(g->display) >= 0) return 1;
    if (errno == EAGAIN) return 0;
    failed(g);
    return -1;
}

/* External desktop state is mandatory; protocol state is an additional veto. */
static bool capable(struct inklow_ghost *g)
{
    return g && !g->failed && !g->hiding && g->compositor && g->shm && g->layers && g->windows &&
           g->outputs && g->viewporter && g->fractional && g->presentation &&
           g->external_desktop_known && !g->external_desktop &&
           (!g->desktop_known || !g->desktop);
}

/* Widen sums to avoid signed-coordinate overflow during containment checks. */
static bool contains(int32_t x, int32_t y, int32_t width, int32_t height,
                     int32_t left, int32_t top, int32_t w, int32_t h)
{
    return width > 0 && height > 0 && w > 0 && h > 0 && left >= x && top >= y &&
           (int64_t)left + w <= (int64_t)x + width &&
           (int64_t)top + h <= (int64_t)y + height;
}

/* Never infer a target from PID alone: require exactly one observed active window. */
int inklow_ghost_window(struct inklow_ghost *g, uint32_t pid, struct inklow_wl_ghost_window *result)
{
    if (!capable(g) || !pid || !result) return 0;
    struct ghost_window *active = NULL;
    for (struct ghost_window *w = g->window_list; w; w = w->next) {
        /* Any not-yet-initialized window makes active-window uniqueness unknown. */
        if (!w->initial || !w->state) return 0;
        if (w->flags & ORG_KDE_PLASMA_WINDOW_MANAGEMENT_STATE_ACTIVE) {
            if (active) return 0;
            active = w;
        }
    }
    if (!active || active->pid != pid || !active->geometry || !active->mapped ||
        (active->flags & (ORG_KDE_PLASMA_WINDOW_MANAGEMENT_STATE_MINIMIZED |
                          ORG_KDE_PLASMA_WINDOW_MANAGEMENT_STATE_SHADED))) return 0;
    *result = (struct inklow_wl_ghost_window){.scene = g->scene, .pid = pid,
        .x = active->x, .y = active->y, .width = active->width, .height = active->height};
    return 1;
}

/* Reject incomplete, overlapping, spanning or transformed output geometry. */
static struct ghost_output *rectangle_output(struct inklow_ghost *g, int x, int y, int width, int height)
{
    struct ghost_output *found = NULL;
    for (struct ghost_output *o = g->output_list; o; o = o->next) {
        if (!o->done || !o->geometry || !o->position || !o->size) return NULL;
        bool intersects = (int64_t)x < (int64_t)o->x + o->width &&
                          (int64_t)y < (int64_t)o->y + o->height &&
                          (int64_t)x + width > o->x && (int64_t)y + height > o->y;
        if (intersects) {
            if (found || o->transform != WL_OUTPUT_TRANSFORM_NORMAL ||
                !contains(o->x, o->y, o->width, o->height, x, y, width, height)) return NULL;
            found = o; /* Complete rectangle in exactly one observed output. */
        }
    }
    return found;
}

static bool current_rectangle(struct inklow_ghost *g)
{
    struct inklow_wl_ghost_window window;
    return inklow_ghost_window(g, g->target_pid, &window) &&
           contains(window.x, window.y, window.width, window.height,
                    g->x, g->y, g->width, g->height) &&
           g->target_output && rectangle_output(g, g->x, g->y, g->width, g->height) == g->target_output;
}

/* Invalidate immediately, then wait for the protocol's output completion fence. */
static void output_dirty(struct ghost_output *o)
{
    o->done = false;
    scene_changed(o->owner);
}

static void output_geometry(void *data, struct wl_output *proxy, int32_t x, int32_t y,
                            int32_t physical_width, int32_t physical_height, int32_t subpixel,
                            const char *make, const char *model, int32_t transform)
{
    (void)proxy; (void)x; (void)y; (void)physical_width; (void)physical_height;
    (void)subpixel; (void)make; (void)model;
    struct ghost_output *o = data;
    if (!o->geometry || o->transform != transform) output_dirty(o);
    o->geometry = true;
    o->transform = transform;
}

static void output_mode(void *data, struct wl_output *proxy, uint32_t flags,
                        int32_t width, int32_t height, int32_t refresh)
{
    (void)proxy; (void)width; (void)height; (void)refresh;
    if (flags & WL_OUTPUT_MODE_CURRENT) output_dirty(data);
}

static void output_done(void *data, struct wl_output *proxy)
{
    (void)proxy;
    struct ghost_output *o = data;
    if (o->logical && zxdg_output_v1_get_version(o->logical) >= 3)
        o->done = o->geometry && o->position && o->size;
}

static void output_scale(void *data, struct wl_output *proxy, int32_t scale)
{
    (void)proxy;
    struct ghost_output *o = data;
    if (!o->scale || o->integer_scale != scale) output_dirty(o);
    o->integer_scale = scale;
    o->scale = true;
    /* Deliberately not used as a pixel ratio: 2 may actually mean 1.25. */
}

static void output_string(void *data, struct wl_output *proxy, const char *value)
{ (void)data; (void)proxy; (void)value; }

static const struct wl_output_listener output_listener = {
    .geometry = output_geometry, .mode = output_mode, .done = output_done,
    .scale = output_scale, .name = output_string, .description = output_string,
};

static void logical_position(void *data, struct zxdg_output_v1 *proxy, int32_t x, int32_t y)
{
    (void)proxy;
    struct ghost_output *o = data;
    if (!o->position || o->x != x || o->y != y) output_dirty(o);
    o->position = true;
    o->x = x; o->y = y;
}

static void logical_size(void *data, struct zxdg_output_v1 *proxy, int32_t width, int32_t height)
{
    (void)proxy;
    struct ghost_output *o = data;
    if (!o->size || o->width != width || o->height != height) output_dirty(o);
    o->width = width; o->height = height;
    o->size = width > 0 && height > 0;
}

static void logical_done(void *data, struct zxdg_output_v1 *proxy)
{
    struct ghost_output *o = data;
    if (zxdg_output_v1_get_version(proxy) < 3) o->done = o->geometry && o->position && o->size;
}

static void logical_string(void *data, struct zxdg_output_v1 *proxy, const char *value)
{ (void)data; (void)proxy; (void)value; }

static const struct zxdg_output_v1_listener logical_listener = {
    .logical_position = logical_position, .logical_size = logical_size, .done = logical_done,
    .name = logical_string, .description = logical_string,
};

/* Registry announcements may arrive in either manager/output order. */
static void add_logical(struct inklow_ghost *g, struct ghost_output *o)
{
    if (g->outputs && !o->logical) {
        o->logical = zxdg_output_manager_v1_get_xdg_output(g->outputs, o->proxy);
        if (!o->logical || zxdg_output_v1_add_listener(o->logical, &logical_listener, o) < 0) failed(g);
    }
}

/* Plasma delivers titles, app IDs and icon names as part of its initial state.
 * They are intentionally ignored, not copied, exposed or logged. */
static void window_string(void *data, struct org_kde_plasma_window *proxy, const char *value)
{ (void)data; (void)proxy; (void)value; }
static void window_number(void *data, struct org_kde_plasma_window *proxy, int32_t value)
{ (void)proxy; (void)value; struct ghost_window *w = data; if (w->flags & 1u) scene_changed(w->owner); }
static void window_empty(void *data, struct org_kde_plasma_window *proxy)
{ (void)data; (void)proxy; }
static void window_menu(void *data, struct org_kde_plasma_window *proxy, const char *service, const char *path)
{ (void)data; (void)proxy; (void)service; (void)path; }
static void window_workspace(void *data, struct org_kde_plasma_window *proxy, const char *value)
{ (void)proxy; (void)value; struct ghost_window *w = data; if (w->flags & 1u) scene_changed(w->owner); }

static void window_state(void *data, struct org_kde_plasma_window *proxy, uint32_t flags)
{
    (void)proxy;
    struct ghost_window *w = data;
    bool changed = !w->state || flags != w->flags;
    w->flags = flags;
    w->state = true;
    if (changed) scene_changed(w->owner);
}

static void window_geometry(void *data, struct org_kde_plasma_window *proxy,
                            int32_t x, int32_t y, uint32_t width, uint32_t height)
{
    (void)proxy;
    struct ghost_window *w = data;
    bool changed = !w->geometry || w->x != x || w->y != y ||
                   (uint32_t)w->width != width || (uint32_t)w->height != height;
    w->x = x; w->y = y;
    w->geometry = width > 0 && height > 0 && width <= INT32_MAX && height <= INT32_MAX;
    w->width = w->geometry ? (int32_t)width : 0;
    w->height = w->geometry ? (int32_t)height : 0;
    if (changed) scene_changed(w->owner);
}

static void window_client_geometry(void *data, struct org_kde_plasma_window *proxy,
                                   int32_t x, int32_t y, uint32_t width, uint32_t height)
{
    /* The public ABI returns the compositor frame. Client insets are never
     * inferred; the bridge independently requires exact client/frame metrics. */
    (void)proxy; (void)x; (void)y; (void)width; (void)height;
    struct ghost_window *w = data;
    if (w->initial && (w->flags & 1u)) scene_changed(w->owner);
}

static void window_pid(void *data, struct org_kde_plasma_window *proxy, uint32_t pid)
{
    (void)proxy;
    struct ghost_window *w = data;
    bool changed = w->pid != pid;
    w->pid = pid;
    if (changed) scene_changed(w->owner);
}

static void window_initial(void *data, struct org_kde_plasma_window *proxy)
{
    struct ghost_window *w = data;
    w->initial = true;
    if (org_kde_plasma_window_get_version(proxy) < 21) w->mapped = true;
    scene_changed(w->owner);
}

static void window_mapped(void *data, struct org_kde_plasma_window *proxy)
{
    (void)proxy;
    struct ghost_window *w = data;
    w->mapped = true;
    scene_changed(w->owner);
}

static void window_parent(void *data, struct org_kde_plasma_window *proxy, struct org_kde_plasma_window *parent)
{
    (void)proxy; (void)parent;
    struct ghost_window *w = data;
    if (w->flags & 1u) scene_changed(w->owner);
}

/* Remove the window before revocation callbacks can query active-window uniqueness. */
static void window_unmapped(void *data, struct org_kde_plasma_window *proxy)
{
    struct ghost_window *w = data;
    struct inklow_ghost *g = w->owner;
    struct ghost_window **link = &g->window_list;
    while (*link && *link != w) link = &(*link)->next;
    if (*link) { *link = w->next; --g->window_count; }
    scene_changed(g);
    org_kde_plasma_window_destroy(proxy);
    erase(w, sizeof(*w));
    free(w);
}

static const struct org_kde_plasma_window_listener window_listener = {
    .title_changed = window_string, .app_id_changed = window_string, .state_changed = window_state,
    .virtual_desktop_changed = window_number, .themed_icon_name_changed = window_string,
    .unmapped = window_unmapped, .initial_state = window_initial, .parent_window = window_parent,
    .geometry = window_geometry, .icon_changed = window_empty, .pid_changed = window_pid,
    .virtual_desktop_entered = window_workspace, .virtual_desktop_left = window_workspace,
    .application_menu = window_menu, .activity_entered = window_workspace, .activity_left = window_workspace,
    .resource_name_changed = window_string, .client_geometry = window_client_geometry, .mapped = window_mapped,
};

static void manager_desktop(void *data, struct org_kde_plasma_window_management *proxy, uint32_t state)
{
    (void)proxy;
    struct inklow_ghost *g = data;
    bool changed = !g->desktop_known || g->desktop != (state != 0);
    g->desktop_known = true;
    g->desktop = state != 0;
    if (changed) scene_changed(g);
}
static void manager_legacy_window(void *data, struct org_kde_plasma_window_management *proxy, uint32_t id)
{ (void)data; (void)proxy; (void)id; }
static void manager_legacy_order(void *data, struct org_kde_plasma_window_management *proxy, struct wl_array *ids)
{
    (void)ids;
    if (org_kde_plasma_window_management_get_version(proxy) < 17) scene_changed(data);
}
static void manager_legacy_uuid(void *data, struct org_kde_plasma_window_management *proxy, const char *ids)
{
    (void)ids;
    if (org_kde_plasma_window_management_get_version(proxy) < 17) scene_changed(data);
}
static void manager_order(void *data, struct org_kde_plasma_window_management *proxy)
{
    (void)proxy;
    /* Includes modal/occlusion changes even if the active PID did not change. */
    scene_changed(data);
}

static void manager_window(void *data, struct org_kde_plasma_window_management *proxy, uint32_t id, const char *uuid)
{
    (void)id;
    struct inklow_ghost *g = data;
    if (g->failed) return;
    if (!uuid || !*uuid || g->window_count >= GHOST_MAX_WINDOWS) { failed(g); return; }
    struct ghost_window *w = calloc(1, sizeof(*w));
    if (!w) { failed(g); return; }
    w->owner = g;
    w->proxy = org_kde_plasma_window_management_get_window_by_uuid(proxy, uuid);
    if (!w->proxy) { free(w); failed(g); return; }
    w->next = g->window_list;
    g->window_list = w;
    ++g->window_count;
    if (org_kde_plasma_window_add_listener(w->proxy, &window_listener, w) < 0) failed(g);
    scene_changed(g);
}

static const struct org_kde_plasma_window_management_listener manager_listener = {
    .show_desktop_changed = manager_desktop, .window = manager_legacy_window,
    .stacking_order_changed = manager_legacy_order, .stacking_order_uuid_changed = manager_legacy_uuid,
    .window_with_uuid = manager_window, .stacking_order_changed_2 = manager_order,
};

static void surface_enter(void *data, struct wl_surface *surface, struct wl_output *output)
{
    struct inklow_ghost *g = data;
    if (surface == g->surface && (!g->target_output || output != g->target_output->proxy)) scene_changed(g);
}
static void surface_leave(void *data, struct wl_surface *surface, struct wl_output *output)
{
    struct inklow_ghost *g = data;
    if (surface == g->surface && g->target_output && output == g->target_output->proxy) scene_changed(g);
}
static const struct wl_surface_listener surface_listener = {.enter = surface_enter, .leave = surface_leave};

/* The first matching configure unlocks readiness; later ones invalidate anchoring. */
static void layer_configure(void *data, struct zwlr_layer_surface_v1 *layer,
                            uint32_t serial, uint32_t width, uint32_t height)
{
    struct inklow_ghost *g = data;
    if (layer != g->layer) return;
    zwlr_layer_surface_v1_ack_configure(layer, serial);
    if ((width && width != (uint32_t)g->width) || (height && height != (uint32_t)g->height)) {
        scene_changed(g);
        return;
    }
    bool initial = !g->configured;
    g->configured = true;
    if (initial) notify(g, 6, 2, g->scene, 0);
    else scene_changed(g); /* External configuration supersedes the old anchor. */
}
static void layer_closed(void *data, struct zwlr_layer_surface_v1 *layer)
{ struct inklow_ghost *g = data; if (layer == g->layer) scene_changed(g); }
static const struct zwlr_layer_surface_v1_listener layer_listener = {.configure = layer_configure, .closed = layer_closed};

static void preferred_scale(void *data, struct wp_fractional_scale_v1 *proxy, uint32_t scale)
{
    struct inklow_ghost *g = data;
    if (proxy != g->surface_scale) return;
    if (!scale || scale > GHOST_MAX_SCALE) { scene_changed(g); return; }
    if (g->scale && scale != g->scale) { scene_changed(g); return; }
    g->scale = scale;
    notify(g, 6, 2, g->scene, 0);
}
static const struct wp_fractional_scale_v1_listener fractional_listener = {.preferred_scale = preferred_scale};

/* Create a fresh noninteractive role, then wait for configure and fractional scale. */
int inklow_ghost_prepare(struct inklow_ghost *g, uint32_t pid, uint64_t scene,
                         int x, int y, int width, int height)
{
    struct inklow_wl_ghost_window window;
    if (!g || !scene || scene != g->scene || width <= 0 || height <= 0 ||
        width > GHOST_MAX_DIMENSION || height > GHOST_MAX_DIMENSION ||
        !inklow_ghost_window(g, pid, &window) ||
        !contains(window.x, window.y, window.width, window.height, x, y, width, height)) {
        inklow_ghost_hide(g);
        return 0;
    }
    struct ghost_output *output = rectangle_output(g, x, y, width, height);
    if (!output) { inklow_ghost_hide(g); return 0; }
    /* Reuse only an identical prepared rectangle. Moving it needs a new role and
     * configure fence, not a buffer commit against stale placement metadata. */
    if (g->surface && g->target_pid == pid && g->target_output == output &&
        g->x == x && g->y == y && g->width == width && g->height == height)
        return g->configured && g->scale ? 1 : 2;
    inklow_ghost_hide(g);
    g->target_pid = pid;
    g->target_output = output;
    g->x = x; g->y = y; g->width = width; g->height = height;
    g->surface = wl_compositor_create_surface(g->compositor);
    if (!g->surface) goto refuse;
    if (wl_surface_add_listener(g->surface, &surface_listener, g) < 0) goto refuse;
    struct wl_region *input = wl_compositor_create_region(g->compositor);
    if (!input) goto refuse;
    wl_surface_set_input_region(g->surface, input); /* Truly empty/click-through. */
    wl_region_destroy(input);
    g->viewport = wp_viewporter_get_viewport(g->viewporter, g->surface);
    g->surface_scale = wp_fractional_scale_manager_v1_get_fractional_scale(g->fractional, g->surface);
    if (!g->viewport || !g->surface_scale) goto refuse;
    if (wp_fractional_scale_v1_add_listener(g->surface_scale, &fractional_listener, g) < 0) goto refuse;
    g->layer = zwlr_layer_shell_v1_get_layer_surface(g->layers, g->surface, output->proxy,
                                                    ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, "brotypist-inline");
    if (!g->layer || zwlr_layer_surface_v1_add_listener(g->layer, &layer_listener, g) < 0) goto refuse;
    zwlr_layer_surface_v1_set_keyboard_interactivity(g->layer, ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
    zwlr_layer_surface_v1_set_exclusive_zone(g->layer, -1);
    zwlr_layer_surface_v1_set_anchor(g->layer, ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT);
    zwlr_layer_surface_v1_set_margin(g->layer, (int32_t)((int64_t)y - output->y), 0, 0,
                                    (int32_t)((int64_t)x - output->x));
    zwlr_layer_surface_v1_set_size(g->layer, (uint32_t)width, (uint32_t)height);
    wp_viewport_set_destination(g->viewport, width, height);
    wl_surface_set_buffer_scale(g->surface, 1);
    wl_surface_commit(g->surface); /* Mandatory no-buffer initial commit. */
    return inklow_ghost_flush(g) < 0 ? 0 : 2;
refuse:
    inklow_ghost_hide(g);
    return 0;
}

int inklow_ghost_scale(struct inklow_ghost *g)
{ return g && !g->failed && g->surface ? (int)g->scale : 0; }

static void buffer_release(void *data, struct wl_buffer *proxy)
{
    (void)proxy;
    struct ghost_buffer *b = data;
    b->busy = false;
    if (b->erase && b->pixels && b->dirty) {
        erase(b->pixels, b->size);
        b->dirty = b->erase = false;
    }
    struct inklow_ghost *g = b->owner;
    if (g && !g->failed && g->surface) notify(g, 6, 2, g->scene, 0);
}
static const struct wl_buffer_listener buffer_listener = {.release = buffer_release};

static void destroy_buffer(struct ghost_buffer *b)
{
    if (b->proxy) wl_buffer_destroy(b->proxy);
    if (b->pixels) {
        /* Busy pixels must not change while the compositor is sampling them.
         * Close drops our last mapping; the backing memfd has no retained fd. */
        if (!b->busy && b->dirty) erase(b->pixels, b->size);
        munmap(b->pixels, b->size);
    }
    memset(b, 0, sizeof(*b));
}

/* Allocate only validated dimensions in an idle slot; retain no backing fd. */
static bool allocate_buffer(struct inklow_ghost *g, struct ghost_buffer *b, int width, int height, int stride)
{
    size_t size = (size_t)stride * (size_t)height;
    int fd = memfd_create("brotypist-inline", MFD_CLOEXEC);
    if (fd < 0) return false;
    if (ftruncate(fd, (off_t)size)) { close(fd); return false; }
    void *pixels = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pixels == MAP_FAILED) { close(fd); return false; }
    struct wl_shm_pool *pool = wl_shm_create_pool(g->shm, fd, (int32_t)size);
    close(fd);
    if (!pool) { munmap(pixels, size); return false; }
    struct wl_buffer *proxy = wl_shm_pool_create_buffer(pool, 0, width, height, stride, WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool);
    if (!proxy) { munmap(pixels, size); return false; }
    *b = (struct ghost_buffer){.owner = g, .proxy = proxy, .pixels = pixels, .size = size,
        .width = width, .height = height, .stride = stride};
    if (wl_buffer_add_listener(proxy, &buffer_listener, b) < 0) { destroy_buffer(b); return false; }
    return true;
}

static void feedback_output(void *data, struct wp_presentation_feedback *feedback, struct wl_output *output)
{
    struct inklow_ghost *g = data;
    if (feedback != g->feedback) return;
    if (!g->target_output || output != g->target_output->proxy) { scene_changed(g); return; }
    g->feedback_output = true;
}

/* Presentation proof requires this frame, its expected output and current geometry. */
static void feedback_presented(void *data, struct wp_presentation_feedback *feedback,
                               uint32_t hi, uint32_t lo, uint32_t nsec, uint32_t refresh,
                               uint32_t seq_hi, uint32_t seq_lo, uint32_t flags)
{
    (void)hi; (void)lo; (void)refresh; (void)seq_hi; (void)seq_lo; (void)flags;
    struct inklow_ghost *g = data;
    if (feedback != g->feedback) return;
    g->feedback = NULL;
    wp_presentation_feedback_destroy(feedback);
    if (!g->visible || !g->configured || !g->feedback_output || nsec >= 1000000000u ||
        g->frame_scene != g->scene || !current_rectangle(g)) {
        inklow_ghost_hide(g);
        return;
    }
    notify(g, 7, 1, g->frame_scene, g->frame);
}

static void feedback_discarded(void *data, struct wp_presentation_feedback *feedback)
{
    struct inklow_ghost *g = data;
    if (feedback != g->feedback) return;
    g->feedback = NULL;
    wp_presentation_feedback_destroy(feedback);
    inklow_ghost_hide(g);
}
static const struct wp_presentation_feedback_listener feedback_listener = {
    .sync_output = feedback_output, .presented = feedback_presented, .discarded = feedback_discarded,
};

/* Queue one monotonically identified frame; a successful flush is not visibility. */
int inklow_ghost_present(struct inklow_ghost *g, uint64_t scene, uint64_t frame,
                         const void *pixels, int width, int height, int stride)
{
    if (!capable(g) || !g->surface || !g->configured || !g->scale || !pixels ||
        scene != g->scene || !frame || frame <= g->last_frame || !current_rectangle(g) ||
        width <= 0 || height <= 0 || width > GHOST_MAX_DIMENSION || height > GHOST_MAX_DIMENSION ||
        width != (int)(((int64_t)g->width * g->scale + 119) / 120) ||
        height != (int)(((int64_t)g->height * g->scale + 119) / 120) ||
        stride < width * 4 || stride % 4 || (size_t)stride * (size_t)height > GHOST_MAX_IMAGE) {
        inklow_ghost_hide(g);
        return 0;
    }
    /* Backpressure is distinct from refusal: busy buffers can be retried after
     * release without allocating an unbounded stream of shared-memory images. */
    struct ghost_buffer *b = NULL;
    for (size_t i = 0; i < 2; ++i) {
        struct ghost_buffer *candidate = &g->buffers[i];
        if (candidate->busy) continue;
        b = candidate;
        if (b->proxy && b->width == width && b->height == height && b->stride == stride) break;
    }
    if (!b) return 2;
    if (!b->proxy || b->width != width || b->height != height || b->stride != stride) {
        destroy_buffer(b);
        if (!allocate_buffer(g, b, width, height, stride)) { inklow_ghost_hide(g); return 0; }
    }
    revoke_frame(g);
    /* Revoke callbacks may synchronously hide through the owning bridge. */
    if (!g->surface || !g->configured || scene != g->scene || !current_rectangle(g)) return 0;
    g->feedback = wp_presentation_feedback(g->presentation, g->surface);
    if (!g->feedback || wp_presentation_feedback_add_listener(g->feedback, &feedback_listener, g) < 0) {
        inklow_ghost_hide(g);
        return 0;
    }
    memcpy(b->pixels, pixels, b->size);
    b->erase = false;
    b->dirty = true;
    g->frame = g->last_frame = frame;
    g->frame_scene = scene;
    wl_surface_attach(g->surface, b->proxy, 0, 0);
    wl_surface_damage(g->surface, 0, 0, g->width, g->height);
    b->busy = g->visible = true;
    wl_surface_commit(g->surface);
    return inklow_ghost_flush(g) < 0 ? 0 : 1;
}

static void presentation_clock(void *data, struct wp_presentation *proxy, uint32_t clock)
{ (void)data; (void)proxy; (void)clock; }
static const struct wp_presentation_listener presentation_listener = {.clock_id = presentation_clock};

static void registry_global(void *data, struct wl_registry *registry, uint32_t id,
                            const char *interface, uint32_t version)
{
    struct inklow_ghost *g = data;
    if (g->failed) return;
    if (!strcmp(interface, "wl_compositor") && !g->compositor && version >= 3) {
        g->compositor = wl_registry_bind(registry, id, &wl_compositor_interface, version < 4 ? version : 4);
        g->compositor_id = id;
    } else if (!strcmp(interface, "wl_shm") && !g->shm) {
        g->shm = wl_registry_bind(registry, id, &wl_shm_interface, 1);
        g->shm_id = id;
    } else if (!strcmp(interface, "zwlr_layer_shell_v1") && !g->layers && version >= 3) {
        g->layers = wl_registry_bind(registry, id, &zwlr_layer_shell_v1_interface, version < 4 ? version : 4);
        g->layers_id = id;
    } else if (!strcmp(interface, "org_kde_plasma_window_management") && !g->windows && version >= 13) {
        /* Current servers require >=17, and are bound at 17..21. Older
         * advertised 13..16 servers have the same UUID/PID/frame evidence and
         * legacy stacking events. Verified against KDE v1.10.0 XML:
         * https://raw.githubusercontent.com/KDE/plasma-wayland-protocols/78fc6ee77334a147986f01c6d3c6e1b99af1a333/src/protocols/plasma-window-management.xml
         */
        g->windows = wl_registry_bind(registry, id, &org_kde_plasma_window_management_interface, version < 21 ? version : 21);
        g->windows_id = id;
        if (!g->windows || org_kde_plasma_window_management_add_listener(g->windows, &manager_listener, g) < 0) failed(g);
    } else if (!strcmp(interface, "zxdg_output_manager_v1") && !g->outputs) {
        g->outputs = wl_registry_bind(registry, id, &zxdg_output_manager_v1_interface, version < 3 ? version : 3);
        g->outputs_id = id;
        for (struct ghost_output *o = g->output_list; o; o = o->next) add_logical(g, o);
    } else if (!strcmp(interface, "wp_viewporter") && !g->viewporter) {
        g->viewporter = wl_registry_bind(registry, id, &wp_viewporter_interface, 1);
        g->viewporter_id = id;
    } else if (!strcmp(interface, "wp_fractional_scale_manager_v1") && !g->fractional) {
        g->fractional = wl_registry_bind(registry, id, &wp_fractional_scale_manager_v1_interface, 1);
        g->fractional_id = id;
    } else if (!strcmp(interface, "wp_presentation") && !g->presentation) {
        g->presentation = wl_registry_bind(registry, id, &wp_presentation_interface, 1);
        g->presentation_id = id;
        if (!g->presentation || wp_presentation_add_listener(g->presentation, &presentation_listener, g) < 0) failed(g);
    } else if (!strcmp(interface, "wl_output") && version >= 2) {
        if (g->output_count >= GHOST_MAX_OUTPUTS) { failed(g); return; }
        struct ghost_output *o = calloc(1, sizeof(*o));
        if (!o) { failed(g); return; }
        o->owner = g; o->id = id;
        o->proxy = wl_registry_bind(registry, id, &wl_output_interface, version < 4 ? version : 4);
        if (!o->proxy) { free(o); failed(g); return; }
        o->next = g->output_list;
        g->output_list = o;
        ++g->output_count;
        if (wl_output_add_listener(o->proxy, &output_listener, o) < 0) failed(g);
        add_logical(g, o);
        scene_changed(g);
    }
}

static void destroy_output(struct ghost_output *o)
{
    if (o->logical) zxdg_output_v1_destroy(o->logical);
    if (wl_output_get_version(o->proxy) >= WL_OUTPUT_RELEASE_SINCE_VERSION) wl_output_release(o->proxy);
    else wl_output_destroy(o->proxy);
    free(o);
}

static void registry_remove(void *data, struct wl_registry *registry, uint32_t id)
{
    (void)registry;
    struct inklow_ghost *g = data;
    for (struct ghost_output **link = &g->output_list; *link; link = &(*link)->next) {
        if ((*link)->id == id) {
            struct ghost_output *o = *link;
            *link = o->next;
            --g->output_count;
            scene_changed(g);
            destroy_output(o);
            return;
        }
    }
    if (id == g->compositor_id || id == g->shm_id || id == g->layers_id || id == g->windows_id ||
        id == g->outputs_id || id == g->viewporter_id || id == g->fractional_id || id == g->presentation_id)
        failed(g); /* Only this optional public renderer stops; private input continues. */
}
static const struct wl_registry_listener registry_listener = {.global = registry_global, .global_remove = registry_remove};

struct inklow_ghost *inklow_ghost_connect(uint32_t compositor_pid, inklow_ghost_notify callback, void *userdata)
{
    const char *name = getenv("WAYLAND_DISPLAY");
    if (!compositor_pid || compositor_pid > INT32_MAX || !name || !*name) return NULL;
    struct inklow_ghost *g = calloc(1, sizeof(*g));
    if (!g) return NULL;
    g->scene = 1;
    /* A non-NULL explicit name ignores WAYLAND_SOCKET, which belongs solely
     * to the private input-method display. Never borrow that fd twice. */
    g->display = wl_display_connect(name);
    if (!g->display) { free(g); return NULL; }
    struct ucred peer;
    socklen_t peer_length = sizeof(peer);
    /* Authenticate before requesting globals or trusting any public state.
     * WAYLAND_DISPLAY may identify a different nested compositor. */
    if (getsockopt(wl_display_get_fd(g->display), SOL_SOCKET, SO_PEERCRED, &peer, &peer_length) ||
        peer_length != sizeof(peer) || peer.pid <= 0 || (uintmax_t)peer.pid != compositor_pid)
        goto refuse;
    g->registry = wl_display_get_registry(g->display);
    if (!g->registry || wl_registry_add_listener(g->registry, &registry_listener, g) < 0) goto refuse;
    /* Registry -> manager/output constructors -> their initial metadata. No
     * roundtrips occur on typing or paint paths. Missing globals are optional. */
    for (unsigned i = 0; i < 3; ++i)
        if (wl_display_roundtrip(g->display) < 0 || g->failed) goto refuse;
    if (!g->compositor || !g->shm || !g->layers || !g->windows || !g->outputs ||
        !g->viewporter || !g->fractional || !g->presentation) goto refuse;
    g->notify = callback;
    g->userdata = userdata;
    return g;
refuse:
    inklow_ghost_close(g);
    return NULL;
}

int inklow_ghost_fd(struct inklow_ghost *g)
{ return g && g->display && !g->failed ? wl_display_get_fd(g->display) : -1; }

int inklow_ghost_dispatch(struct inklow_ghost *g)
{
    if (!g || !g->display || g->failed) return -1;
    /* Bound callback draining; leave more queued work for the same Qt owner. */
    unsigned attempts = 0;
    while (wl_display_prepare_read(g->display) != 0) {
        if (wl_display_dispatch_pending(g->display) < 0 || g->failed) { failed(g); return -1; }
        if (++attempts == 4) return inklow_ghost_flush(g);
    }
    struct pollfd fd = {.fd = wl_display_get_fd(g->display), .events = POLLIN};
    int result;
    do { result = poll(&fd, 1, 0); } while (result < 0 && errno == EINTR);
    if (result < 0 || (fd.revents & (POLLERR | POLLHUP | POLLNVAL))) {
        wl_display_cancel_read(g->display);
        failed(g);
        return -1;
    }
    if (fd.revents & POLLIN) {
        if (wl_display_read_events(g->display) < 0) { failed(g); return -1; }
    } else wl_display_cancel_read(g->display);
    if (wl_display_dispatch_pending(g->display) < 0 || g->failed) { failed(g); return -1; }
    return inklow_ghost_flush(g);
}

/* Suppress notifications before teardown can revoke frames or encounter errors. */
void inklow_ghost_close(struct inklow_ghost *g)
{
    if (!g) return;
    g->notify = NULL;
    inklow_ghost_hide(g);
    for (size_t i = 0; i < 2; ++i) destroy_buffer(&g->buffers[i]);
    while (g->window_list) {
        struct ghost_window *w = g->window_list;
        g->window_list = w->next;
        org_kde_plasma_window_destroy(w->proxy);
        erase(w, sizeof(*w));
        free(w);
    }
    while (g->output_list) {
        struct ghost_output *o = g->output_list;
        g->output_list = o->next;
        destroy_output(o);
    }
    if (g->presentation) wp_presentation_destroy(g->presentation);
    if (g->fractional) wp_fractional_scale_manager_v1_destroy(g->fractional);
    if (g->viewporter) wp_viewporter_destroy(g->viewporter);
    if (g->outputs) zxdg_output_manager_v1_destroy(g->outputs);
    if (g->windows) org_kde_plasma_window_management_destroy(g->windows);
    if (g->layers) zwlr_layer_shell_v1_destroy(g->layers);
    if (g->shm) wl_shm_destroy(g->shm);
    if (g->compositor) wl_compositor_destroy(g->compositor);
    if (g->registry) wl_registry_destroy(g->registry);
    if (g->display) { wl_display_flush(g->display); wl_display_disconnect(g->display); }
    erase(g, sizeof(*g));
    free(g);
}

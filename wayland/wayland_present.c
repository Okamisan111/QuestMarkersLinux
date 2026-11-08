/*
 * wayland_present - dumb ARGB frame presenter for the quest overlay.
 *
 * A transparent, always-on-top, click-through layer-shell surface on one
 * monitor. Reads raw premultiplied ARGB8888 frames (B,G,R,A byte order, the
 * cairo ARGB32 layout) from stdin and commits them.
 *
 * Why: on this KWin/Wayland setup XWayland refuses depth-32 windows (BadMatch
 * on XCreateWindow) and shape-clipped X11 windows stay opaque black outside
 * the clip, so real per-pixel alpha needs a native Wayland surface.
 *
 * usage: wayland_present WIDTH HEIGHT [OUTPUT_X] [SELECTOR]
 *        OUTPUT_X < 0 -> compositor default output
 *        SELECTOR picks the monitor: "#1" = --list index, "1280,0" = layout
 *        position, anything else = wl_output name or a fragment of its
 *        description ("DP-1", "ULTRAGEAR"). It wins over OUTPUT_X. Compositors
 *        that send no wl_output name (KWin does not) are picked by index.
 *        wayland_present --list -> one JSON line per monitor, then exit
 */
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <wayland-client.h>

#include "wlr-layer-shell-client-protocol.h"

#define MAX_OUTPUTS 8
#define NBUFS 2

struct output {
    struct wl_output *output;
    int32_t x, y, w, h;      /* x/y: layout position, w/h: current mode */
    int32_t scale;
    int32_t refresh;         /* mHz of the current mode */
    char name[64];           /* wl_output name (v2): "DP-1", "eDP-1", ... */
    char description[128];   /* wl_output description, else "make model" */
    int have_geometry;
    int done;                /* compositor finished describing this output */
};

struct state {
    struct wl_display *dpy;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct zwlr_layer_shell_v1 *layer_shell;
    struct wl_surface *surface;
    struct zwlr_layer_surface_v1 *layer_surface;
    struct wl_output *target;

    struct output outputs[MAX_OUTPUTS];
    int n_outputs;
    int want_x;          /* legacy: match by layout x offset */
    int want_index;      /* #N: match by --list enumeration index */
    int want_gx, want_gy, want_geo;  /* "x,y": match by layout position */
    char want_name[128]; /* name, or a case-insensitive description fragment */

    struct wl_buffer *buf[NBUFS];
    unsigned char *data[NBUFS];
    int busy[NBUFS];
    int cur;

    int width, height;
    size_t frame_bytes;
    int configured;
};

static struct state st;

#define MAXRECTS 2048

struct rect_def {
    int32_t x, y, w, h, stride;
};

/* rects committed on each buffer two frames ago — the exact set that must be
 * cleared when the buffer becomes free again, so untouched pixels stay
 * transparent and no full-frame memset is ever needed. */
static struct rect_def buf_rects[NBUFS][MAXRECTS];
static int buf_nrects[NBUFS];

/* bounding box of everything changed on the buffer being filled this frame
 * (cleared rects + newly painted rects) — damage only that, so the compositor
 * blends the marker area, not the full 14 MB surface */
static int bb_x0, bb_y0, bb_x1, bb_y1;

static void bb_extend(int x0, int y0, int x1, int y1)
{
    if (x0 < bb_x0) bb_x0 = x0;
    if (y0 < bb_y0) bb_y0 = y0;
    if (x1 > bb_x1) bb_x1 = x1;
    if (y1 > bb_y1) bb_y1 = y1;
}

static int read_all(int fd, unsigned char *p, size_t n)
{
    while (n > 0) {
        ssize_t r = read(fd, p, n);
        if (r == 0)
            return -1; /* EOF */
        if (r < 0) {
            if (errno == EINTR)
                continue;
            perror("read");
            return -1;
        }
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

static void clear_rects(int slot)
{
    unsigned char *base = st.data[slot];
    size_t row = (size_t)st.width * 4;
    for (int i = 0; i < buf_nrects[slot]; i++) {
        struct rect_def *r = &buf_rects[slot][i];
        int x0 = r->x < 0 ? 0 : r->x;
        int y0 = r->y < 0 ? 0 : r->y;
        int x1 = r->x + r->w > st.width ? st.width : r->x + r->w;
        int y1 = r->y + r->h > st.height ? st.height : r->y + r->h;
        /* records may come from a previous, LARGER surface generation (KWin
         * shrinks our layer surface on output mode churn while Python keeps
         * painting at its fixed W,H): the clipped x1 can fall behind x0 ->
         * (x1 - x0) * 4 would wrap in size_t and memset 40TB into a 3MB
         * mapping (observed SIGSEGV). Same guard as the write path. */
        if (x1 <= x0 || y1 <= y0)
            continue;
        for (int y = y0; y < y1; y++)
            memset(base + (size_t)y * row + (size_t)x0 * 4, 0,
                   (size_t)(x1 - x0) * 4);
        bb_extend(x0, y0, x1, y1);
    }
    buf_nrects[slot] = 0;
}

static int create_shm_file(size_t size)
{
    int fd = (int)syscall(SYS_memfd_create, "quest-overlay", MFD_CLOEXEC);
    if (fd < 0)
        return -1;
    if (ftruncate(fd, (off_t)size) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static void buffer_release(void *data, struct wl_buffer *buffer)
{
    int *slot = data;
    (void)buffer;
    *slot = 0;
}

static const struct wl_buffer_listener buffer_listener = {
    .release = buffer_release,
};

/* previous-generation buffers/mappings: on a compositor reconfigure (mode or
 * output change during a raid) they must die explicitly. A still-alive old
 * buffer carries a release listener pointing at &st.busy[i]; when that late
 * release arrives it would clear the busy flag of the NEW buffer the
 * compositor is still displaying -> double commit of an unreleased buffer
 * (protocol violation, observed as presenter SIGSEGV/respawn storms).
 * Deferred destroy of busy buffers is protocol-safe; the listener is removed
 * at destroy time, so no stale release can ever hit st.busy again. */
static void free_old_buffers(void)
{
    for (int i = 0; i < NBUFS; i++) {
        if (st.buf[i]) {
            wl_buffer_destroy(st.buf[i]);
            st.buf[i] = NULL;
        }
        if (st.data[i]) {
            munmap(st.data[i], st.frame_bytes);
            st.data[i] = NULL;
        }
        st.busy[i] = 0;
        buf_nrects[i] = 0; /* new buffers are zero-filled: nothing to clear */
    }
}

static int alloc_buffers(void)
{
    st.frame_bytes = (size_t)st.width * (size_t)st.height * 4;
    for (int i = 0; i < NBUFS; i++) {
        int fd = create_shm_file(st.frame_bytes);
        if (fd < 0) {
            fprintf(stderr, "memfd_create failed: %s\n", strerror(errno));
            return -1;
        }
        void *p = mmap(NULL, st.frame_bytes, PROT_READ | PROT_WRITE,
                       MAP_SHARED, fd, 0);
        if (p == MAP_FAILED) {
            fprintf(stderr, "mmap failed: %s\n", strerror(errno));
            close(fd);
            return -1;
        }
        struct wl_shm_pool *pool = wl_shm_create_pool(st.shm, fd,
                                                      (int32_t)st.frame_bytes);
        st.buf[i] = wl_shm_pool_create_buffer(pool, 0, st.width, st.height,
                                              st.width * 4,
                                              WL_SHM_FORMAT_ARGB8888);
        wl_shm_pool_destroy(pool);
        close(fd);
        if (!st.buf[i]) {
            fprintf(stderr, "wl_shm_pool_create_buffer failed\n");
            return -1;
        }
        st.data[i] = p;
        st.busy[i] = 0;
        wl_buffer_add_listener(st.buf[i], &buffer_listener, &st.busy[i]);
    }
    return 0;
}

static void layer_surface_configure(void *data,
                                    struct zwlr_layer_surface_v1 *ls,
                                    uint32_t serial, uint32_t w, uint32_t h)
{
    (void)data;
    zwlr_layer_surface_v1_ack_configure(ls, serial);
    if (w > 0 && h > 0) {
        st.width = (int)w;
        st.height = (int)h;
    }
    if (st.frame_bytes == 0 || (size_t)st.width * st.height * 4 != st.frame_bytes) {
        free_old_buffers();
        if (alloc_buffers() < 0) {
            fprintf(stderr, "buffer allocation failed\n");
            exit(1);
        }
    }
    st.configured = 1;
    /* Live surface size goes to the overlay on stdout (it drains and relays
     * this line): feed nx/ny are fractions of the game viewport, so the
     * overlay remaps them onto the real surface when KWin fractional scaling
     * or a mode change resizes this layer - markers keep their placement. */
    printf("configured %dx%d\n", st.width, st.height);
    fflush(stdout);
}

static void layer_surface_closed(void *data, struct zwlr_layer_surface_v1 *ls)
{
    (void)data;
    (void)ls;
    fprintf(stderr, "layer surface closed by compositor\n");
    exit(0);
}

static const struct zwlr_layer_surface_v1_listener layer_surface_listener = {
    .configure = layer_surface_configure,
    .closed = layer_surface_closed,
};

static void output_geometry(void *data, struct wl_output *output, int32_t x,
                            int32_t y, int32_t pw, int32_t ph, int32_t subpixel,
                            const char *make, const char *model,
                            int32_t transform)
{
    (void)output;
    (void)pw;   /* physical size: useless for picking, the mode tells the truth */
    (void)ph;
    (void)subpixel;
    (void)transform;
    struct output *o = data;
    o->x = x;
    o->y = y;
    o->have_geometry = 1;
    /* fallback label until (and unless) a wl_output description arrives */
    if (o->description[0] == '\0' && make && make[0])
        snprintf(o->description, sizeof(o->description), "%s %s",
                 make, model ? model : "");
}

static void output_mode(void *data, struct wl_output *output, uint32_t flags,
                        int32_t w, int32_t h, int32_t refresh)
{
    (void)output;
    struct output *o = data;
    if (flags & WL_OUTPUT_MODE_CURRENT) {
        o->w = w;
        o->h = h;
        o->refresh = refresh;
    }
}

static void output_done(void *data, struct wl_output *output)
{
    (void)output;
    ((struct output *)data)->done = 1;
}

static void output_scale(void *data, struct wl_output *output, int32_t factor)
{
    (void)output;
    ((struct output *)data)->scale = factor > 0 ? factor : 1;
}

/* wl_output v2 events: the stable identity the user picks a monitor by */
static void output_name(void *data, struct wl_output *output, const char *name)
{
    (void)output;
    struct output *o = data;
    snprintf(o->name, sizeof(o->name), "%s", name ? name : "");
}

static void output_description(void *data, struct wl_output *output,
                               const char *desc)
{
    (void)output;
    struct output *o = data;
    snprintf(o->description, sizeof(o->description), "%s", desc ? desc : "");
}

static const struct wl_output_listener output_listener = {
    .geometry = output_geometry,
    .mode = output_mode,
    .done = output_done,
    .scale = output_scale,
    .name = output_name,
    .description = output_description,
};

static void registry_global(void *data, struct wl_registry *registry,
                            uint32_t name, const char *iface, uint32_t version)
{
    (void)data;
    if (strcmp(iface, wl_compositor_interface.name) == 0) {
        st.compositor = wl_registry_bind(registry, name, &wl_compositor_interface,
                                         version < 4 ? version : 4);
    } else if (strcmp(iface, wl_shm_interface.name) == 0) {
        st.shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    } else if (strcmp(iface, zwlr_layer_shell_v1_interface.name) == 0) {
        st.layer_shell = wl_registry_bind(registry, name,
                                          &zwlr_layer_shell_v1_interface,
                                          version < 4 ? version : 4);
    } else if (strcmp(iface, wl_output_interface.name) == 0) {
        if (st.n_outputs >= MAX_OUTPUTS)
            return;
        struct output *o = &st.outputs[st.n_outputs];
        o->output = wl_registry_bind(registry, name, &wl_output_interface,
                                     version < 2 ? version : 2);
        o->have_geometry = 0;
        o->w = o->h = 0;
        wl_output_add_listener(o->output, &output_listener, o);
        st.n_outputs++;
    }
}

static void registry_global_remove(void *data, struct wl_registry *registry,
                                   uint32_t name)
{
    (void)data;
    (void)registry;
    (void)name;
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

static int o_strcasecmp(const char *a, const char *b)
{
    if (!a || !b || !a[0] || !b[0])
        return 1;              /* an unnamed output is not the one asked for */
    return strcasecmp(a, b);
}

static void json_escape(const char *src, char *dst, size_t n)
{
    size_t j = 0;
    if (!src || n < 3) { if (n) dst[0] = '\0'; return; }
    for (const unsigned char *p = (const unsigned char *)src;
         *p && j + 2 < n; p++) {
        unsigned char c = *p;
        if (c == '"' || c == '\\') { dst[j++] = '\\'; dst[j++] = (char)c; }
        else if (c == '\n' || c == '\r' || c == '\t') {
            dst[j++] = '\\';
            dst[j++] = c == '\n' ? 'n' : (c == '\r' ? 'r' : 't');
        } else if (c < 0x20) {
            j += (size_t)snprintf(dst + j, n - j, "\\u%04x", c);
        } else {
            dst[j++] = (char)c;   /* UTF-8 monitor names pass through */
        }
    }
    dst[j] = '\0';
}

static void log_outputs(void)
{
    for (int i = 0; i < st.n_outputs; i++) {
        struct output *o = &st.outputs[i];
        fprintf(stderr, "output[%d]: name=%s desc=%s %dx%d%+d%+d scale=%d\n",
                i, o->name[0] ? o->name : "(none)",
                o->description[0] ? o->description : "(none)",
                o->w, o->h, o->x, o->y, o->scale);
    }
}

static void print_outputs_json(void)
{
    for (int i = 0; i < st.n_outputs; i++) {
        struct output *o = &st.outputs[i];
        char nm[160], ds[320];
        json_escape(o->name, nm, sizeof(nm));
        json_escape(o->description, ds, sizeof(ds));
        printf("{\"index\":%d,\"name\":\"%s\",\"description\":\"%s\",\"x\":%d,"
               "\"y\":%d,\"width\":%d,\"height\":%d,\"scale\":%d,"
               "\"refresh_mhz\":%d,\"done\":%d}\n",
               i, nm, ds, o->x, o->y, o->w, o->h, o->scale > 0 ? o->scale : 1,
               o->refresh, o->done ? 1 : 0);
    }
    fflush(stdout);
}

static int desc_match(const char *desc, const char *sel)
{
    if (!desc || !desc[0] || !sel || !sel[0])
        return 0;
    return strcasestr(desc, sel) != NULL;
}

static void pick_output(void)
{
    st.target = NULL;
    int i;

    /* An index is what the Python side falls back to when the compositor
     * sends no monitor name, and it refers to the very same enumeration it
     * just read from --list in this Wayland session. */
    if (st.want_index >= 0) {
        if (st.want_index < st.n_outputs) {
            st.target = st.outputs[st.want_index].output;
            fprintf(stderr, "output: index %d (%s %dx%d%+d%+d)\n",
                    st.want_index,
                    st.outputs[st.want_index].name[0]
                        ? st.outputs[st.want_index].name
                        : (st.outputs[st.want_index].description[0]
                           ? st.outputs[st.want_index].description : "unnamed"),
                    st.outputs[st.want_index].w, st.outputs[st.want_index].h,
                    st.outputs[st.want_index].x, st.outputs[st.want_index].y);
            return;
        }
        fprintf(stderr, "output: no monitor at index %d (%d seen)\n",
                st.want_index, st.n_outputs);
        log_outputs();
        fprintf(stderr, "output: falling back to the compositor default\n");
        return;
    }
    if (st.want_geo) {
        for (i = 0; i < st.n_outputs; i++) {
            if (st.outputs[i].have_geometry && st.outputs[i].x == st.want_gx
                && st.outputs[i].y == st.want_gy) {
                st.target = st.outputs[i].output;
                fprintf(stderr, "output: position %d,%d (index %d)\n",
                        st.want_gx, st.want_gy, i);
                return;
            }
        }
        fprintf(stderr, "output: no monitor at %d,%d\n", st.want_gx, st.want_gy);
        log_outputs();
        fprintf(stderr, "output: falling back to the compositor default\n");
        return;
    }

    /* A monitor name beats an x offset: the layout offset changes the moment
     * someone plugs in or rearranges a screen, the connector name does not. */
    if (st.want_name[0]) {
        for (int i = 0; i < st.n_outputs; i++) {
            if (o_strcasecmp(st.outputs[i].name, st.want_name) == 0
                || desc_match(st.outputs[i].description, st.want_name)) {
                st.target = st.outputs[i].output;
                fprintf(stderr, "output: name %s (index %d, %dx%d%+d%+d)\n",
                        st.outputs[i].name, i, st.outputs[i].w,
                        st.outputs[i].h, st.outputs[i].x, st.outputs[i].y);
                return;
            }
        }
        fprintf(stderr, "output: no monitor named %s\n", st.want_name);
        log_outputs();
        fprintf(stderr, "output: falling back to the compositor default\n");
        return;
    }
    if (st.want_x < 0) {
        fprintf(stderr, "output: compositor default\n");
        return;
    }
    for (int i = 0; i < st.n_outputs; i++) {
        if (st.outputs[i].have_geometry && st.outputs[i].x == st.want_x) {
            st.target = st.outputs[i].output;
            fprintf(stderr, "output: x=%d (index %d)\n", st.outputs[i].x, i);
            return;
        }
    }
    fprintf(stderr, "output: no output at x=%d\n", st.want_x);
    log_outputs();
    fprintf(stderr, "output: falling back to the compositor default\n");
}

int main(int argc, char **argv)
{
    int list_only = (argc >= 2 && strcmp(argv[1], "--list") == 0);
    if (!list_only && argc < 3) {
        fprintf(stderr, "usage: %s WIDTH HEIGHT [OUTPUT_X] [OUTPUT_NAME]\n"
                        "       %s --list\n", argv[0], argv[0]);
        return 2;
    }

    st.dpy = wl_display_connect(NULL);
    if (!st.dpy) {
        fprintf(stderr, "cannot connect to Wayland display\n");
        return list_only ? 3 : 1;
    }
    struct wl_registry *registry = wl_display_get_registry(st.dpy);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    wl_display_roundtrip(st.dpy); /* globals */
    wl_display_roundtrip(st.dpy); /* output geometry/mode/name/description */
    /* A compositor that batches its output state can still be mid-batch
     * after two roundtrips; one more is cheap and makes --list complete. */
    if (list_only && st.n_outputs) {
        for (int pass = 0; pass < 6; pass++) {
            int pending = 0;
            for (int i = 0; i < st.n_outputs; i++)
                if (!st.outputs[i].done)
                    pending++;
            if (!pending)
                break;
            if (wl_display_roundtrip(st.dpy) < 0)
                break;
        }
        print_outputs_json();
        wl_display_disconnect(st.dpy);
        return st.n_outputs ? 0 : 3;
    }
    if (list_only) {
        fprintf(stderr, "no wl_output globals on this display\n");
        wl_display_disconnect(st.dpy);
        return 3;
    }

    st.width = atoi(argv[1]);
    st.height = atoi(argv[2]);
    st.want_x = argc > 3 ? atoi(argv[3]) : -1;
    st.want_index = -1;
    if (argc > 4) {
        const char *sel = argv[4];
        const char *comma = strchr(sel, ',');
        int consumed = 0;
        if (sel[0] == '#') {
            st.want_index = atoi(sel + 1);
        } else if (comma
                   && sscanf(sel, "%d,%d%n", &st.want_gx, &st.want_gy,
                             &consumed) == 2
                   && consumed == (int)strlen(sel)) {
            st.want_geo = 1;
        } else {
            snprintf(st.want_name, sizeof(st.want_name), "%s", sel);
        }
    }
    st.cur = 0;

    if (!st.compositor || !st.shm || !st.layer_shell) {
        fprintf(stderr, "missing compositor/shm/layer_shell globals\n");
        return 1;
    }
    pick_output();

    st.surface = wl_compositor_create_surface(st.compositor);
    st.layer_surface = zwlr_layer_shell_v1_get_layer_surface(
        st.layer_shell, st.surface, st.target,
        ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, "quest-overlay");

    /* anchor to all edges -> fills the whole output */
    zwlr_layer_surface_v1_set_anchor(st.layer_surface,
                                     ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP |
                                     ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
                                     ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT |
                                     ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    zwlr_layer_surface_v1_set_size(st.layer_surface, 0, 0);
    zwlr_layer_surface_v1_set_exclusive_zone(st.layer_surface, -1);
    zwlr_layer_surface_v1_set_keyboard_interactivity(
        st.layer_surface,
        ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
    zwlr_layer_surface_v1_add_listener(st.layer_surface,
                                      &layer_surface_listener, NULL);

    /* empty input region -> clicks pass through to the game */
    struct wl_region *empty = wl_compositor_create_region(st.compositor);
    wl_surface_set_input_region(st.surface, empty);
    wl_region_destroy(empty);
    wl_surface_commit(st.surface);

    while (!st.configured) {
        if (wl_display_dispatch(st.dpy) < 0) {
            fprintf(stderr, "wayland dispatch failed\n");
            return 1;
        }
    }

    /* announce readiness on stdout so the Python side can start feeding */
    printf("ready %dx%d\n", st.width, st.height);
    fflush(stdout);

    size_t payload_cap = 0;
    size_t frame_seen = 0;   /* summed rect payload bytes of one frame */
    unsigned char *payload = NULL;
    int desync = 0;

    for (;;) {
        /* find a buffer the compositor is done with */
        int slot = -1;
        for (int i = 0; i < NBUFS; i++) {
            int cand = (st.cur + i) % NBUFS;
            if (!st.busy[cand]) {
                slot = cand;
                break;
            }
        }
        if (slot < 0) {
            wl_display_flush(st.dpy);
            if (wl_display_dispatch(st.dpy) < 0)
                break;
            continue;
        }
        st.cur = slot;
        if (!st.data[slot] || !st.buf[slot]) {
            /* allocation race: wait for the next configure instead of
             * writing into a freed or never-allocated buffer */
            if (wl_display_dispatch(st.dpy) < 0)
                break;
            continue;
        }

        unsigned char type;
        if (read_all(0, &type, 1) < 0)
            break;
        bb_x0 = st.width;
        bb_y0 = st.height;
        bb_x1 = 0;
        bb_y1 = 0;
        clear_rects(slot);
        frame_seen = 0;

        if (type == 'F') {
            uint32_t n;
            int32_t hdr[5];
            if (read_all(0, (unsigned char *)&n, sizeof(n)) < 0)
                break;
            if (n > MAXRECTS) {
                desync = 1;
                break;
            }
            for (uint32_t i = 0; i < n; i++) {
                if (read_all(0, (unsigned char *)hdr, sizeof(hdr)) < 0) {
                    desync = 1;
                    break;
                }
                struct rect_def r = { hdr[0], hdr[1], hdr[2], hdr[3], hdr[4] };
                size_t len = (size_t)r.stride * (size_t)r.h;
                if (r.stride <= 0 || r.h <= 0 || r.w <= 0) {
                    desync = 1;
                    break;
                }
                if ((size_t)r.stride < 4u * (size_t)r.w) {
                    /* stride smaller than the row it claims to carry would let
                     * the clipped memcpy read past the payload row boundary */
                    desync = 1;
                    break;
                }
                /* Cap derived from the live surface (st.frame_bytes) instead
                 * of a flat 17 MiB: the overlay splits merged rects at its
                 * surface size, so a rect bigger than this surface is hostile
                 * or corrupt - while 4K legitimately needs up to 33 MB. */
                if (len > st.frame_bytes) {
                    desync = 1;
                    break;
                }
                frame_seen += len;
                if (frame_seen > 2 * st.frame_bytes) {
                    /* disjoint rects cannot legitimately cover more than the
                     * surface area; two surfaces' worth bounds hostile sums */
                    desync = 1;
                    break;
                }
                if (len > payload_cap) {
                    payload = realloc(payload, len);
                    if (!payload) {
                        desync = 1;
                        break;
                    }
                    payload_cap = len;
                }
                if (read_all(0, payload, len) < 0) {
                    desync = 1;
                    break;
                }
                /* row-wise copy into the buffer, clipped to the surface */
                size_t srow = (size_t)st.width * 4;
                for (int row = 0; row < r.h; row++) {
                    int sy = r.y + row;
                    if (sy < 0 || sy >= st.height)
                        continue;
                    int x0 = r.x, x1 = r.x + r.w;
                    int col0 = 0;
                    if (x0 < 0) {
                        x0 = 0;
                        col0 = -r.x;
                    }
                    if (x1 > st.width)
                        x1 = st.width;
                    if (x1 <= x0)
                        continue;
                    memcpy(st.data[slot] + (size_t)sy * srow + (size_t)x0 * 4,
                           payload + (size_t)row * r.stride + (size_t)col0 * 4,
                           (size_t)(x1 - x0) * 4);
                }
                int cy0 = r.y < 0 ? 0 : r.y;
                int cy1 = r.y + r.h > st.height ? st.height : r.y + r.h;
                int cx0 = r.x < 0 ? 0 : r.x;
                int cx1 = r.x + r.w > st.width ? st.width : r.x + r.w;
                if (cx1 > cx0 && cy1 > cy0) {
                    bb_extend(cx0, cy0, cx1, cy1);
                    if (buf_nrects[slot] < MAXRECTS) {
                        /* store the CLIPPED footprint: clear_rects then works
                         * on coordinates that are always inside whatever
                         * surface generation is currently allocated */
                        struct rect_def clipped = { cx0, cy0, cx1 - cx0,
                                                    cy1 - cy0, r.stride };
                        buf_rects[slot][buf_nrects[slot]++] = clipped;
                    }
                }
            }
            if (desync)
                break;
        } else if (type != 'C') {
            fprintf(stderr, "unknown message type 0x%02x\n", type);
            break;
        }

        st.busy[slot] = 1;
        wl_surface_attach(st.surface, st.buf[slot], 0, 0);
        if (bb_x1 > bb_x0 && bb_y1 > bb_y0)
            wl_surface_damage_buffer(st.surface, bb_x0, bb_y0,
                                     bb_x1 - bb_x0, bb_y1 - bb_y0);
        else
            wl_surface_damage_buffer(st.surface, 0, 0, st.width, st.height);
        wl_surface_commit(st.surface);
        wl_display_flush(st.dpy);
    }

    fprintf(stderr, "input closed, exiting\n");
    return 0;
}

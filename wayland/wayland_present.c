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
 * usage: wayland_present WIDTH HEIGHT OUTPUT_X
 *        OUTPUT_X < 0 -> compositor default output
 */
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <wayland-client.h>

#include "wlr-layer-shell-client-protocol.h"

#define MAX_OUTPUTS 8
#define NBUFS 2

struct output {
    struct wl_output *output;
    int32_t x, y, w, h;
    int have_geometry;
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
    int want_x;

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
    (void)pw;
    (void)ph;
    (void)subpixel;
    (void)make;
    (void)model;
    (void)transform;
    struct output *o = data;
    o->x = x;
    o->y = y;
    o->have_geometry = 1;
}

static void output_mode(void *data, struct wl_output *output, uint32_t flags,
                        int32_t w, int32_t h, int32_t refresh)
{
    (void)output;
    (void)refresh;
    struct output *o = data;
    if (flags & WL_OUTPUT_MODE_CURRENT) {
        o->w = w;
        o->h = h;
    }
}

static void output_done(void *data, struct wl_output *output)
{
    (void)data;
    (void)output;
}

static void output_scale(void *data, struct wl_output *output, int32_t factor)
{
    (void)data;
    (void)output;
    (void)factor;
}

static const struct wl_output_listener output_listener = {
    .geometry = output_geometry,
    .mode = output_mode,
    .done = output_done,
    .scale = output_scale,
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

static void pick_output(void)
{
    st.target = NULL;
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
    fprintf(stderr, "output: no output at x=%d, using compositor default\n",
            st.want_x);
}

int main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: %s WIDTH HEIGHT OUTPUT_X\n", argv[0]);
        return 2;
    }
    st.width = atoi(argv[1]);
    st.height = atoi(argv[2]);
    st.want_x = atoi(argv[3]);
    st.cur = 0;

    st.dpy = wl_display_connect(NULL);
    if (!st.dpy) {
        fprintf(stderr, "cannot connect to Wayland display\n");
        return 1;
    }
    struct wl_registry *registry = wl_display_get_registry(st.dpy);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    wl_display_roundtrip(st.dpy); /* globals */
    wl_display_roundtrip(st.dpy); /* output geometry */

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
                if (r.stride <= 0 || r.h <= 0) {
                    desync = 1;
                    break;
                }
                if (len > payload_cap) {
                    if (len > 17u * 1024 * 1024) {
                        /* Hard ceiling: the overlay enforces len <= frame_bytes
                         * (14.7 MB at 4K), so 17 MiB covers legitimate large
                         * surfaces while a bogus header stays memory-bounded. */
                        desync = 1;
                        break;
                    }
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

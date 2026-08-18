/*
 * What the GPU stack actually resolves to, from inside a workspace.
 *
 * Benchmarks under llvmpipe are not slow versions of the real thing, they are
 * a different measurement entirely -- so "is this the real GPU" has to be a
 * check with an answer, not an assumption. The image cannot install eglinfo
 * (workspaces are offline by design), so this asks EGL directly, using the
 * same platforms WPE itself would: a Wayland display when there is one, the
 * DRM render node through GBM otherwise, and EGL_EXT_platform_device as the
 * last resort, which is the one NVIDIA supports without a compositor.
 *
 * Prints one line per successful platform:
 *
 *   platform=wayland vendor=... renderer=... version=...
 *
 * Exit status: 0 if any platform gave a hardware renderer, 1 if only software
 * (llvmpipe, softpipe, swrast) came back, 2 if EGL produced nothing at all.
 *
 * Build:  cc -o gpu-probe gpu-probe.c -lEGL -lGLESv2 -lgbm
 */

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <gbm.h>

static int saw_hardware = 0;
static int saw_any = 0;

static int is_software(const char *renderer)
{
    return strcasestr(renderer, "llvmpipe") || strcasestr(renderer, "softpipe")
        || strcasestr(renderer, "swrast")   || strcasestr(renderer, "lavapipe");
}

static void report(const char *platform, EGLDisplay dpy)
{
    EGLint major, minor;
    if (!eglInitialize(dpy, &major, &minor)) {
        printf("platform=%-10s unavailable (eglInitialize failed)\n", platform);
        return;
    }

    /* A context is required before GL_RENDERER means anything: the vendor
     * string on the display alone does not say which driver will run. */
    static const EGLint cfg_attr[] = {
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_NONE
    };
    static const EGLint ctx_attr[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };

    EGLConfig config;
    EGLint n = 0;
    eglBindAPI(EGL_OPENGL_ES_API);
    if (!eglChooseConfig(dpy, cfg_attr, &config, 1, &n) || n < 1) {
        printf("platform=%-10s unavailable (no config)\n", platform);
        eglTerminate(dpy);
        return;
    }

    EGLContext ctx = eglCreateContext(dpy, config, EGL_NO_CONTEXT, ctx_attr);
    if (ctx == EGL_NO_CONTEXT) {
        printf("platform=%-10s unavailable (no context)\n", platform);
        eglTerminate(dpy);
        return;
    }

    if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) {
        printf("platform=%-10s unavailable (makeCurrent failed)\n", platform);
        eglDestroyContext(dpy, ctx);
        eglTerminate(dpy);
        return;
    }

    const char *vendor   = (const char *)glGetString(GL_VENDOR);
    const char *renderer = (const char *)glGetString(GL_RENDERER);
    const char *version  = (const char *)glGetString(GL_VERSION);

    saw_any = 1;
    if (renderer && !is_software(renderer))
        saw_hardware = 1;

    printf("platform=%-10s vendor=%s | renderer=%s | version=%s%s\n",
           platform, vendor ? vendor : "?", renderer ? renderer : "?",
           version ? version : "?",
           (renderer && is_software(renderer)) ? "   [SOFTWARE]" : "");

    eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(dpy, ctx);
    eglTerminate(dpy);
}

int main(void)
{
    PFNEGLGETPLATFORMDISPLAYEXTPROC get_display =
        (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");

    /* The path a WPE benchmark run actually takes: a Wayland client of the
     * compositor holding the monitor. */
    const char *wayland = getenv("WAYLAND_DISPLAY");
    if (wayland && get_display) {
        EGLDisplay dpy = get_display(EGL_PLATFORM_WAYLAND_EXT, NULL, NULL);
        if (dpy != EGL_NO_DISPLAY)
            report("wayland", dpy);
        else
            printf("platform=wayland    unavailable (no display for %s)\n", wayland);
    }

    /* The render node directly, which is what tells you whether the device is
     * reachable at all when no compositor is running. */
    const char *node = getenv("WK_RENDER_NODE");
    if (!node)
        node = "/dev/dri/renderD128";
    int fd = open(node, O_RDWR | O_CLOEXEC);
    if (fd >= 0 && get_display) {
        struct gbm_device *gbm = gbm_create_device(fd);
        if (gbm) {
            EGLDisplay dpy = get_display(EGL_PLATFORM_GBM_KHR, gbm, NULL);
            if (dpy != EGL_NO_DISPLAY)
                report("gbm", dpy);
        } else {
            printf("platform=gbm        unavailable (gbm_create_device failed on %s)\n", node);
        }
    } else if (fd < 0) {
        printf("platform=gbm        unavailable (cannot open %s)\n", node);
    }

    /* NVIDIA without a compositor: no GBM needed, the device platform is
     * enough to prove the driver is loaded and usable. */
    PFNEGLQUERYDEVICESEXTPROC query_devices =
        (PFNEGLQUERYDEVICESEXTPROC)eglGetProcAddress("eglQueryDevicesEXT");
    if (query_devices && get_display) {
        EGLDeviceEXT devices[8];
        EGLint count = 0;
        if (query_devices(8, devices, &count) && count > 0) {
            EGLDisplay dpy = get_display(EGL_PLATFORM_DEVICE_EXT, devices[0], NULL);
            if (dpy != EGL_NO_DISPLAY)
                report("device", dpy);
        }
    }

    if (!saw_any) {
        fprintf(stderr, "gpu-probe: EGL produced no usable display at all\n");
        return 2;
    }
    return saw_hardware ? 0 : 1;
}

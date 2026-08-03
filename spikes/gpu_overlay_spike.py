"""
GPU overlay spike — de-risks TICKET-092 (Pygame-CE + SDL2 + OpenGL/moderngl GPU renderer)
BEFORE committing to the full render-layer rewrite.

It proves (or disproves) the ONE unknown that decides everything: can a borderless
SDL/OpenGL window do PER-PIXEL ALPHA transparency (DWM route) AND click-through
(WS_EX_TRANSPARENT) AND topmost-over-a-game (no focus steal) — together, on Windows,
from Python? Everything else (60fps glyph-atlas scroll) is already proven; this is not.

RUN IT (deps on the D-drive env per the local-ai-rig convention):
    pip install pygame-ce moderngl
    python spikes/gpu_overlay_spike.py

You'll see a soft glowing shape floating over your desktop. Use the keys below.

PASS / FAIL (this is the whole bet):
  SPIKE 0 — transparency: you see the DESKTOP through the transparent pixels and the
            shape has a CLEAN ANTI-ALIASED edge (not a black box, not a grey haze).
  SPIKE 1 — click-through (press [SPACE] to toggle): with it ON, clicks land in the app
            BEHIND the overlay AND the shape STILL renders with per-pixel alpha.
            >>> If the alpha turns BLACK the moment click-through turns on, per-pixel
                alpha + WS_EX_TRANSPARENT do NOT cohabit on pygame here. That is the
                gate: fall back to colorkey transparency (== exactly what main.py does
                today at the -transparentcolor line, still ships the 60fps win, just no
                soft-glow), or re-evaluate GLFW/QOpenGLWidget. NOT a blocker for perf.
  SPIKE 2 — topmost / no focus steal (press [T] to toggle): launch a BORDERLESS-fullscreen
            game (or borderless YouTube fullscreen). The overlay stays visibly on top and
            never steals focus / keyboard from the game.

KEYS:  [SPACE] click-through on/off   [T] topmost on/off   [ESC] quit
Console prints FPS once a second.

KNOWN LIMIT (unchanged from today, true of EVERY engine): a true EXCLUSIVE-fullscreen
DirectX game cannot be overlaid by any Win32 window without DXGI/Present hooks. This
targets borderless-fullscreen-windowed, which is what main.py already supports.
"""
from __future__ import annotations
import ctypes
import sys
import time
from ctypes import wintypes

import pygame
import moderngl

W, H = 900, 520

# ── win32 / dwmapi plumbing ─────────────────────────────────────────────────────
user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

DWM_BB_ENABLE = 0x01
DWM_BB_BLURREGION = 0x02


class DWM_BLURBEHIND(ctypes.Structure):
    _fields_ = [("dwFlags", wintypes.DWORD), ("fEnable", wintypes.BOOL),
                ("hRgnBlur", wintypes.HRGN), ("fTransitionOnMaximized", wintypes.BOOL)]


def enable_per_pixel_alpha(hwnd):
    """DWM 'blur-behind with an EMPTY region' = honor the GL framebuffer's alpha
    channel for per-pixel desktop compositing, with NO GPU->CPU readback (avoid the
    UpdateLayeredWindow/GDI path — it would crater FPS)."""
    region = gdi32.CreateRectRgn(0, 0, -1, -1)   # empty region
    bb = DWM_BLURBEHIND(DWM_BB_ENABLE | DWM_BB_BLURREGION, True, region, False)
    dwmapi.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(bb))
    gdi32.DeleteObject(region)


def set_exstyle(hwnd, click_through: bool, topmost: bool):
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if click_through:
        ex |= WS_EX_TRANSPARENT
    else:
        ex &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
    user32.SetWindowPos(hwnd, HWND_TOPMOST if topmost else HWND_NOTOPMOST,
                        0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


# ── premultiplied-alpha soft shape shader ───────────────────────────────────────
VERT = """
#version 330
in vec2 in_pos;
out vec2 uv;
void main() { uv = in_pos; gl_Position = vec4(in_pos, 0.0, 1.0); }
"""
# Outputs PREMULTIPLIED color (rgb already * a). A soft rounded glow so the AA edge
# and per-pixel alpha are obvious against the desktop.
FRAG = """
#version 330
in vec2 uv;
out vec4 frag;
uniform float t;
void main() {
    vec2 p = uv;
    float r = length(p * vec2(1.0, 1.3));
    float a = smoothstep(0.85, 0.45, r);              // soft AA falloff edge
    a *= 0.55 + 0.45 * sin(t * 1.5);                  // pulse so it's clearly live
    vec3 rgb = mix(vec3(0.95, 0.30, 0.55), vec3(0.30, 0.55, 1.0), 0.5 + 0.5 * p.y);
    frag = vec4(rgb * a, a);                           // premultiplied
}
"""


def main():
    pygame.init()
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_ALPHA_SIZE, 8)         # ask for an alpha channel
    pygame.display.set_mode((W, H), pygame.OPENGL | pygame.DOUBLEBUF | pygame.NOFRAME)
    pygame.display.set_caption("gpu_overlay_spike")

    hwnd = pygame.display.get_wm_info()["window"]
    enable_per_pixel_alpha(hwnd)
    click_through = False
    topmost = True
    set_exstyle(hwnd, click_through, topmost)

    ctx = moderngl.create_context()
    print("GL_RENDERER:", ctx.info.get("GL_RENDERER"))   # SPIKE 4: which GPU got the context
    prog = ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
    # fullscreen triangle
    import array
    vbo = ctx.buffer(array.array("f", [-1, -1, 3, -1, -1, 3]).tobytes())
    vao = ctx.vertex_array(prog, [(vbo, "2f", "in_pos")])

    ctx.enable(moderngl.BLEND)
    # premultiplied-alpha blend; GL_MAX on the alpha channel so AA edges keep full alpha
    ctx.blend_func = (moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA,
                      moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA)
    ctx.blend_equation = (moderngl.FUNC_ADD, moderngl.MAX)

    clock = pygame.time.Clock()
    t0 = time.perf_counter()
    last_report = t0
    frames = 0
    guard = 0.0
    running = True
    print("running — [SPACE] click-through  [T] topmost  [ESC] quit")
    while running:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    running = False
                elif e.key == pygame.K_SPACE:
                    click_through = not click_through
                    set_exstyle(hwnd, click_through, topmost)
                    print(f"click-through = {click_through}")
                elif e.key == pygame.K_t:
                    topmost = not topmost
                    set_exstyle(hwnd, click_through, topmost)
                    print(f"topmost = {topmost}")

        now = time.perf_counter()
        # self-heal: re-assert the exstyle bits ~2x/sec (SDL can reset them on events),
        # mirroring main.py's _click_guard.
        if now - guard > 0.5:
            guard = now
            set_exstyle(hwnd, click_through, topmost)

        ctx.clear(0.0, 0.0, 0.0, 0.0)        # fully transparent
        prog["t"].value = now - t0
        vao.render(mode=moderngl.TRIANGLES)
        pygame.display.flip()

        frames += 1
        if now - last_report >= 1.0:
            print(f"fps {frames / (now - last_report):5.1f}   "
                  f"click_through={click_through}  topmost={topmost}")
            frames = 0
            last_report = now
        clock.tick(120)   # vsync usually caps this; tick is a backstop

    pygame.quit()


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("Windows-only spike (DWM + win32 ex-styles).")
    main()

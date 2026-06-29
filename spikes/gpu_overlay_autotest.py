r"""Auto-bounded driver for gpu_overlay_spike — runs the GL overlay for ~7s, captures
the COMPOSITED screen through the window at click-through OFF then ON, prints GL_RENDERER
+ FPS, then quits. The screenshots let us SEE the answer to the only open question:
does borderless GL per-pixel-alpha transparency survive WS_EX_TRANSPARENT click-through?
"""
from __future__ import annotations
import array
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pygame
import moderngl
from PIL import ImageGrab
from gpu_overlay_spike import enable_per_pixel_alpha, set_exstyle, VERT, FRAG, W, H

SCRATCH = r"C:\Users\user\AppData\Local\Temp\claude\D--\1e3fdef9-0dbd-496e-8160-ecbb524749bf\scratchpad"


def grab(hwnd, name):
    user32 = ctypes.windll.user32
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    bbox = (r.left, r.top, r.right, r.bottom)
    try:
        img = ImageGrab.grab(bbox=bbox)
        os.makedirs(SCRATCH, exist_ok=True)
        img.save(os.path.join(SCRATCH, name))
        # quick black-box check: mean luminance of a centre patch
        small = img.convert("L").resize((48, 48))
        px = list(small.getdata())
        nonblack = sum(1 for p in px if p > 12) / len(px)
        return bbox, round(nonblack, 3)
    except Exception as e:
        return bbox, f"grab-failed: {e}"


def main():
    pygame.init()
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_ALPHA_SIZE, 8)
    pygame.display.set_mode((W, H), pygame.OPENGL | pygame.DOUBLEBUF | pygame.NOFRAME)
    pygame.display.set_caption("gpu_overlay_autotest")
    hwnd = pygame.display.get_wm_info()["window"]
    enable_per_pixel_alpha(hwnd)
    set_exstyle(hwnd, click_through=False, topmost=True)

    ctx = moderngl.create_context()
    print("GL_RENDERER:", ctx.info.get("GL_RENDERER"))
    print("GL_VENDOR  :", ctx.info.get("GL_VENDOR"))
    prog = ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
    vbo = ctx.buffer(array.array("f", [-1, -1, 3, -1, -1, 3]).tobytes())
    vao = ctx.vertex_array(prog, [(vbo, "2f", "in_pos")])
    ctx.enable(moderngl.BLEND)
    ctx.blend_func = (moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA,
                      moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA)
    ctx.blend_equation = (moderngl.FUNC_ADD, moderngl.MAX)

    clock = pygame.time.Clock()
    t0 = time.perf_counter()
    frames = 0
    grabbed_off = grabbed_on = False
    click_through = False
    guard = 0.0
    running = True
    while running:
        now = time.perf_counter()
        el = now - t0
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
        if now - guard > 0.5:
            guard = now
            set_exstyle(hwnd, click_through, True)   # self-heal the ex-styles
        ctx.clear(0.0, 0.0, 0.0, 0.0)
        prog["t"].value = el
        vao.render(mode=moderngl.TRIANGLES)
        pygame.display.flip()
        frames += 1
        if el > 2.0 and not grabbed_off:
            grabbed_off = True
            bbox, nb = grab(hwnd, "gpu_spike_clickthrough_off.png")
            print(f"[SPIKE0] click-through OFF — grabbed {bbox} nonblack_frac={nb}")
        if el > 3.0 and not click_through:
            click_through = True
            set_exstyle(hwnd, True, True)
            print("[SPIKE1] click-through -> ON (WS_EX_TRANSPARENT)")
        if el > 5.0 and not grabbed_on:
            grabbed_on = True
            bbox, nb = grab(hwnd, "gpu_spike_clickthrough_on.png")
            print(f"[SPIKE1] click-through ON  — grabbed {bbox} nonblack_frac={nb}")
        if el > 6.5:
            running = False
        clock.tick(120)
    dt = time.perf_counter() - t0
    print(f"FPS ~{frames / dt:.0f} over {frames} frames / {dt:.1f}s")
    pygame.quit()
    print("done")


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("Windows-only")
    main()

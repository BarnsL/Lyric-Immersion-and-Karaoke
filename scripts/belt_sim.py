"""Offline belt-motion simulation — faithfully ports _eased_offset + the belt
delta so we can measure the "lurch" (per-frame belt velocity spikes) under a
realistic schedule of sync corrections, and compare configs. Smooth belt =
velocity ratio ~1.0 every frame; a lurch = ratio spikes >1.

Metric: velocity ratio = d(display_pos)/dt normalised by realtime (1.0 = glued to
real playback). We report the worst ratio and how many frames exceed 1.5 (visible)
and 2.0 (obvious lurch) over the run.
"""
import math

FPS_DT = 1/60.0

def ease_step(cur, target, dt, slew_cap=3.0, pull=3.5, max_frac=0.20, deadzone=0.05, snap=12.0):
    if abs(target - cur) > snap:  return target
    if abs(target - cur) <= deadzone: return target
    delta = target - cur
    step = delta * (1.0 - math.exp(-pull * dt))
    cap = slew_cap * dt
    step = max(-cap, min(cap, step))
    if max_frac > 0 and delta != 0:
        fc = max_frac * abs(delta); sign = 1.0 if step >= 0 else -1.0
        step = sign * min(abs(step), fc)
    return cur + step

def run(label, apply_floor, corrections, heavy_frames=(), T=60.0,
        slew_cap=3.0, pull=3.5):
    """corrections: list of (t_seconds, proposed_drift). apply_floor gates them.
    heavy_frames: set of frame indices with a 0.3s stall (models a Tk hitch)."""
    n = int(T / FPS_DT)
    offset = 0.0                 # committed sync target
    disp = 0.0                   # eased display offset
    prev_pos = 0.0
    t = 0.0
    corr = sorted(corrections)
    ci = 0
    worst, over15, over20 = 1.0, 0, 0
    ratios = []
    for i in range(n):
        dt = FPS_DT
        if i in heavy_frames:
            dt = 0.30            # a 300ms hitch: the render thread stalled
        t += dt
        # apply any due correction (gated by the floor — this is the deadband)
        while ci < len(corr) and corr[ci][0] <= t:
            _, drift = corr[ci]; ci += 1
            if abs(drift) >= apply_floor:     # else: skipped (belt stays smooth)
                offset += drift               # commit toward the heard target
        disp = ease_step(disp, offset, dt, slew_cap=slew_cap, pull=pull)
        pos = t + disp                        # realtime + eased offset (feeds the belt)
        # belt velocity ratio vs realtime (1.0 = perfectly glued/smooth)
        if i > 0:
            ratio = (pos - prev_pos) / dt
            ratios.append(ratio)
            worst = max(worst, ratio)
            if ratio > 1.5: over15 += 1
            if ratio > 2.0: over20 += 1
        prev_pos = pos
    import statistics
    sd = statistics.pstdev(ratios) if ratios else 0
    print(f"  {label:38s} worst_vel={worst:4.2f}x  frames>1.5x={over15:3d}  >2.0x={over20:3d}  vel_std={sd:.3f}")

# Realistic schedule: small ongoing drift corrections every ~12s (the belt-lurchers),
# plus one genuine big re-anchor at t=30s, plus a couple of heavy Tk frames.
CORR = [(8, 0.4), (12, -0.5), (20, 0.6), (25, -0.35), (30, 1.6), (42, 0.5), (50, -0.6), (55, 0.45)]
HEAVY = {600, 1800}   # frame 600 (~10s) and 1800 (~30s): a 300ms hitch each

print("Belt lurch under a realistic 60s correction schedule (lower worst/std = smoother):")
run("A baseline (floor 0.22, ease 3.0/3.5)",       0.22, CORR, HEAVY)
run("B deadband (floor 1.0, ease 3.0/3.5)",        1.00, CORR, HEAVY)
run("C deadband + gentle ease (1.0, 1.5/2.0)",     1.00, CORR, HEAVY, slew_cap=1.5, pull=2.0)
run("D deadband + gentler (1.0, 1.0/1.5)",         1.00, CORR, HEAVY, slew_cap=1.0, pull=1.5)
print("\n(worst_vel = fastest single-frame belt speed vs realtime; 1.0 = perfectly smooth.\n"
      " frames>1.5x/2.0x = visibly-speeding frames. The big t=30s re-anchor (1.6s) is >floor\n"
      " in BOTH, so it still glides — that's the intended behavior.)")

"""Reproducible evaluation of concert MC (talk) handling — spec 001.

WHAT IT DOES
------------
Builds labelled "mini-concerts" from permissively-licensed recordings plus
synthetic applause / crowd murmur / arena reverb, then runs the offline concert
analyser (concert_audio.py) twice, with MC detection OFF (the pre-spec-001
behaviour) and ON, and prints:

  1. the share of each kind of audio flagged as MC talk (talk, songs, applause,
     instrumentals);
  2. where chapter onsets land when a chapter opens with talk (the bug: onsets
     on the talk);
  3. how a chapterless concert is segmented (the bug: one merged "song");
  4. with --songs: seconds of FALSE MC inside each of 22 real songs (incl. rap)
     and MC recall on talk under several mixing conditions.

LICENSING (constitution Principle I — nothing here is committed)
------------------------------------------------------------------
Audio is downloaded at runtime into --cache (default: a temporary directory that
is deleted afterwards). Only commercially usable licences are fetched:

  github.com/librosa/data (audio/):
    LibriSpeech 5703-47212-0000, 3436-172162-0000, 198-209-0000   CC BY 4.0
    Karissa Hobbs - Let's Go Fishin'                               CC BY 3.0
    Kevin MacLeod - Vibe Ace                                       CC BY 3.0
    Brahms - Hungarian Dance No. 5 (US Army Strings, Musopen)      Public Domain
  github.com/f90/jamendolyrics (mp3/): every track whose LicenseType has no NC
    clause (CC BY, BY-SA, BY-ND), per its JamendoLyrics.csv.

NC-licensed clips in those repositories are deliberately skipped.

USAGE
-----
    python scripts/eval_concert_mc.py                  # mini-concerts only (~10 MB)
    python scripts/eval_concert_mc.py --songs          # + 22 real songs (~110 MB)
    python scripts/eval_concert_mc.py --cache .eval_audio --songs

Needs numpy plus PyAV / faster-whisper (decoding and the Silero speech model),
i.e. the same optional stack the concert analyser uses in the app.
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import concert_audio as ca  # noqa: E402

SR = ca._SR
LIBROSA = "https://raw.githubusercontent.com/librosa/data/main/audio/"
JAMENDO = "https://raw.githubusercontent.com/f90/jamendolyrics/master/"
CLIPS = {
    "libri1": "5703-47212-0000.hq.ogg",
    "libri2": "3436-172162-0000.hq.ogg",
    "libri3": "198-209-0000.hq.ogg",
    "fishin": "Karissa_Hobbs_-_Lets_Go_Fishin.hq.ogg",
    "vibeace": "Kevin_MacLeod_-_Vibe_Ace.hq.ogg",
    "brahms": "Hungarian_Dance_number_5_-_Allegro_in_F_sharp_minor_(string_orchestra).hq.ogg",
}


# ── fetching / decoding ──────────────────────────────────────────────────────
def fetch(url, dest):
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def decode(path):
    pcm, _how = ca._decode(path)
    if pcm is None:
        raise RuntimeError(f"cannot decode {path}")
    return ca._as_f32(pcm)


class Clips:
    def __init__(self, cache):
        self.cache, self._memo = cache, {}

    def __call__(self, name):
        if name not in self._memo:
            self._memo[name] = decode(fetch(LIBROSA + CLIPS[name], self.cache / CLIPS[name]))
        return self._memo[name]


# ── synthesis of the concert "room" ──────────────────────────────────────────
rng = np.random.default_rng(1234)


def rms(x):
    return float(np.sqrt(np.mean(np.square(x, dtype="float64")) + 1e-12))


def at_level(x, target):
    return (x * (target / max(rms(x), 1e-9))).astype("float32")


def reverb(x, rt60=1.6, wet=0.35):
    """Arena-ish reverb: an exponentially decaying noise impulse response."""
    n = int(SR * rt60)
    t = np.arange(n) / SR
    ir = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    ir[0] = 0.0
    ir /= np.sqrt(np.sum(ir ** 2))
    m = len(x) + n - 1
    nfft = 1 << (m - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(ir, nfft), nfft)[:len(x)]
    return ((1 - wet) * x + wet * at_level(y.astype("float32"), rms(x))).astype("float32")


def pink(n):
    w = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    f[0] = 1.0
    return np.fft.irfft(w / np.sqrt(f), n).astype("float32")


def applause(seconds, level=0.08, density=400.0):
    """Dense random claps (short noise bursts) + a diffuse bed + a few whoops."""
    n = int(seconds * SR)
    out = np.zeros(n, "float32")
    for _ in range(int(density * seconds)):
        L = int(SR * rng.uniform(0.004, 0.015))
        s = int(rng.integers(0, max(1, n - L)))
        out[s:s + L] += (rng.standard_normal(L) * np.hanning(L) * rng.uniform(0.2, 1.0)).astype("float32")
    out = np.diff(np.concatenate([[0.0], out])).astype("float32")
    out += 0.3 * at_level(pink(n), rms(out) + 1e-9)
    for _ in range(int(seconds / 2.5) + 1):
        L = min(n, int(SR * rng.uniform(0.6, 1.4)))
        s = int(rng.integers(0, max(1, n - L)))
        f0 = rng.uniform(350, 900)
        ph = 2 * np.pi * np.cumsum(np.linspace(f0, f0 * rng.uniform(0.8, 1.4), L)) / SR
        out[s:s + L] += (0.25 * (np.sin(ph) + 0.5 * np.sin(2 * ph)) * np.hanning(L)
                         * rms(out) / 0.3).astype("float32")
    return at_level(out, level)


def talk(clips, names=("libri1", "libri2", "libri3"), level=0.10, bgm=None, bgm_db=-14.0,
         murmur_db=-24.0, cheers=True, rt60=1.4, wet=0.3):
    """MC talk: speech with natural pauses, reverb, crowd murmur, occasional
    short cheers between sentences, optional music bed underneath."""
    parts = []
    for nm in names:
        parts.append(clips(nm))
        parts.append(np.zeros(int(SR * rng.uniform(0.4, 1.4)), "float32"))
    sp = reverb(at_level(np.concatenate(parts), level), rt60=rt60, wet=wet)
    n = len(sp)
    out = sp + at_level(pink(n), level * 10 ** (murmur_db / 20))
    if bgm is not None:
        b = np.tile(clips(bgm), int(np.ceil(n / len(clips(bgm)))))[:n]
        out += at_level(b, level * 10 ** (bgm_db / 20))
    if cheers:
        s = 0
        for p in parts[::2]:
            s += len(p)
            if rng.random() < 0.5 and s + SR < n:
                c = applause(1.2, level=level * 0.6, density=250)
                out[s:s + len(c)] += c[:n - s]
    return out.astype("float32")


def song(clips, name, level=0.12, start_s=0.0, dur_s=None):
    x = clips(name)
    a = int(start_s * SR)
    b = len(x) if dur_s is None else min(len(x), a + int(dur_s * SR))
    return reverb(at_level(x[a:b], level), rt60=1.2, wet=0.15)


def assemble(blocks):
    pcm, spans, t = [], [], 0.0
    for lab, a in blocks:
        pcm.append(a)
        spans.append((lab, t, t + len(a) / SR))
        t += len(a) / SR
    return np.concatenate(pcm).astype("float32"), spans


def label_at(spans, t):
    for lab, s, e in spans:
        if s <= t < e:
            return lab
    return "?"


def mini_concert(clips):
    return assemble([
        ("applause", applause(8.0)),
        ("talk", talk(clips, ("libri1", "libri2"))),
        ("song", song(clips, "fishin", dur_s=70.0)),
        ("applause", applause(6.0)),
        ("talk+bgm", talk(clips, ("libri3", "libri1"), bgm="vibeace")),
        ("instrumental", song(clips, "brahms")),
        ("applause", applause(5.0)),
        ("talk", talk(clips, ("libri2", "libri3"))),
        ("song", song(clips, "fishin", start_s=63.0, dur_s=70.0)),
        ("applause", applause(7.0)),
    ])


# ── reports ──────────────────────────────────────────────────────────────────
def report_mini(clips):
    pcm, spans = mini_concert(clips)
    print(f"\n== 1. mini-concert ({len(pcm) / SR:.0f} s): share of each kind flagged as MC talk")
    n = len(pcm) // int(SR * ca._HOP_S)
    mask, mc, info = ca._mc_intervals(pcm, n)
    by, tot = collections.Counter(), collections.Counter()
    for i in range(n):
        lab = label_at(spans, (i + 0.5) * ca._HOP_S)
        tot[lab] += 1
        by[lab] += int(mask[i])
    for lab in sorted(tot):
        print(f"   {lab:13s} {by[lab] / tot[lab]:6.0%}")
    print(f"   detector: {info['detector']}")

    print("\n== 2. chapters that OPEN WITH TALK: where the lyric onset lands")
    chapters = [{"start": s, "title": f"Chapter {i}"}
                for i, (lab, s, e) in enumerate(spans) if lab.startswith("talk")]
    for label, tune in (("MC detection OFF (before)", {"concert_mc_detect": 0}),
                        ("MC detection ON  (after) ", {})):
        res = ca.analyze_concert(pcm=pcm, chapters=chapters, want_ids=False, tune=tune)
        lands = [label_at(spans, s["onset"]) for s in res["segments"]]
        bad = sum(1 for x in lands if x.startswith("talk"))
        print(f"   {label}: onsets land in {lands}  → {bad}/{len(lands)} on talk")

    print("\n== 3. chapterless concert: segments found")
    for label, tune in (("MC detection OFF (before)", {"concert_mc_detect": 0}),
                        ("MC detection ON  (after) ", {})):
        res = ca.analyze_concert(pcm=pcm, want_ids=False, tune=tune)
        desc = []
        for s in res["segments"]:
            kinds = collections.Counter(label_at(spans, t) for t in
                                        np.arange(s["start"], s["end"], ca._HOP_S))
            desc.append(f"{s['start']:.0f}-{s['end']:.0f}s {dict(kinds.most_common(3))}")
        print(f"   {label}: {len(res['segments'])} segment(s), {len(res['mc'])} MC interval(s)")
        for d in desc:
            print(f"      {d}")


def jamendo_songs(cache):
    csv_path = fetch(JAMENDO + "JamendoLyrics.csv", cache / "JamendoLyrics.csv")
    rows = list(csv.DictReader(io.open(csv_path, encoding="utf-8")))
    ok = [r for r in rows if "NC" not in r["LicenseType"]]
    out = []
    for r in ok:
        fn = r["Filepath"]
        out.append((f"{r['Genre']:9s} {r['Artist'][:24]} - {r['Title'][:28]}",
                    fetch(JAMENDO + "mp3/" + urllib.parse.quote(fn), cache / "jamendo" / fn)))
    return out


def report_songs(clips, cache):
    print("\n== 4a. real songs: seconds of FALSE MC talk inside each song (want 0)")
    total_fp, total_s = 0.0, 0.0
    songs = jamendo_songs(cache) + [("librosa   Let's Go Fishin'", None)]
    for name, path in songs:
        x = decode(path) if path is not None else clips("fishin")
        n = len(x) // int(SR * ca._HOP_S)
        mask, _mc, _ = ca._mc_intervals(x, n)
        fp = float(mask.sum()) * ca._HOP_S
        total_fp += fp
        total_s += len(x) / SR
        print(f"   {fp:6.1f} s  {name}")
    print(f"   TOTAL {total_fp:.1f} s of {total_s:.0f} s of songs ({100 * total_fp / total_s:.2f} %)")

    print("\n== 4b. MC recall: share of talk flagged, per mixing condition (3 seeds each)")
    conds = {
        "dry": dict(murmur_db=-30.0, cheers=False),
        "hall + cheers": dict(murmur_db=-24.0),
        "loud crowd": dict(murmur_db=-15.0),
        "BGM -20 dB": dict(bgm="vibeace", bgm_db=-20.0),
        "BGM -14 dB": dict(bgm="vibeace", bgm_db=-14.0),
        "BGM -8 dB": dict(bgm="vibeace", bgm_db=-8.0),
        "strings BGM -14 dB": dict(bgm="brahms", bgm_db=-14.0),
        "arena reverb (extreme)": dict(murmur_db=-15.0, rt60=2.5, wet=0.45),
    }
    global rng
    for cname, kw in conds.items():
        vals = []
        for seed in (1, 2, 3):
            rng = np.random.default_rng(100 + seed)
            x = talk(clips, **kw)
            n = len(x) // int(SR * ca._HOP_S)
            mask, _mc, _ = ca._mc_intervals(x, n)
            vals.append(float(mask.mean()))
        print(f"   {cname:24s} {np.mean(vals):6.0%}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", type=Path, default=None,
                    help="keep downloaded audio here (default: temp dir, deleted)")
    ap.add_argument("--songs", action="store_true",
                    help="also evaluate false MC on 22 real songs + recall conditions")
    args = ap.parse_args()
    if not ca.available():
        sys.exit("needs numpy + PyAV / faster-whisper (the concert analyser's optional stack)")
    tmp = None
    cache = args.cache
    if cache is None:
        tmp = Path(tempfile.mkdtemp(prefix="eval_concert_mc_"))
        cache = tmp
    try:
        clips = Clips(cache)
        report_mini(clips)
        if args.songs:
            report_songs(clips, cache)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

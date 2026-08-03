"""PE-import-table guard for PyAV's FFmpeg DLLs — the DIRECT detector for the
TICKET-176 whisper breakage.

TICKET-176 RCA (docs/ISSUES.md): faster-whisper imports PyAV, whose C-extension
`av/_core.pyd` is linked against a set of delvewheel-mangled FFmpeg DLLs
(`avformat-62-<hash>.dll`, `avcodec-<n>-<hash>.dll`, …) that ship in the sibling
`av.libs` folder. The mangled name embeds a per-BUILD hash, so a `_core.pyd` from
av 17.1.0 needs `avformat-62-9c2d3ee….dll` while an `av.libs` from av 18.0.0 has
`avformat-62-b6d6bb16….dll` — a DIFFERENT file. When PyInstaller bundled a
`_core.pyd` from a stale vendored `.deps` alongside an `av.libs` collected from a
newer environment, the import table pointed at DLLs that WEREN'T THERE and
`import av` died with "DLL load failed while importing _core". That silently
returned `align.available()` False → generate-by-ear, sync-by-listening AND the
wrong-lyrics reject path all degraded to no-ops, in every shipped build v1.1.74→76,
with nothing in the log.

A VERSION check (scripts/check_build_deps.py) is only a PROXY — dist-info metadata
can lag the real module files, and the failure is fundamentally about DLL FILE
IDENTITY, not version strings. This checks the thing that actually breaks: it reads
the PE import table of every `av/*.pyd` and asserts that EVERY FFmpeg-family DLL
they import is present as a file in `av.libs`. If one is missing, the frozen
`import av` WILL fail — so the build must fail here, loudly.

No third-party dependency: the PE import directory is parsed with stdlib `struct`
(equivalent to pefile's DIRECTORY_ENTRY_IMPORT) so this guard can never be silently
skipped because `pefile` wasn't installed on a given build machine.

Usage
-----
    python scripts/check_av_dlls.py                # pre-build: check .deps/av (or env av)
    python scripts/check_av_dlls.py --source       #   (same — explicit)
    python scripts/check_av_dlls.py --internal DIR  # post-build: check the bundled av
    python scripts/check_av_dlls.py <dist-dir>      #   (positional = --internal)

Exit 0 = consistent (or nothing to check — a lean build with no PyAV). Exit 1 =
a `_core.pyd` imports an FFmpeg DLL that is NOT in `av.libs` → the build is broken.
"""
from __future__ import annotations

import glob
import os
import struct
import sys

# FFmpeg / PyAV native libs the extension modules link against. delvewheel renames
# them to "<base>-<soname>-<hash>.dll"; we match on the base prefix, case-insensitively.
_FFMPEG_PREFIXES = (
    "avformat", "avcodec", "avutil", "avfilter", "avdevice",
    "swscale", "swresample", "postproc",
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pe_imported_dlls(path: str) -> list[str]:
    """Return the list of DLL names in `path`'s PE import table (IMAGE_DIRECTORY_
    ENTRY_IMPORT). Pure stdlib — mirrors pefile's DIRECTORY_ENTRY_IMPORT[*].dll.
    Raises ValueError on a malformed PE (caller decides how loud to be)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"MZ":
        raise ValueError("not a PE file (no MZ header)")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("not a PE file (no PE signature)")
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    size_opt = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x20B:          # PE32+  (64-bit) — the av *.pyd case
        dd_off = opt + 112
    elif magic == 0x10B:        # PE32   (32-bit)
        dd_off = opt + 96
    else:
        raise ValueError(f"unknown optional-header magic 0x{magic:x}")
    # Data directory [1] = Import Table (RVA, size)
    import_rva = struct.unpack_from("<I", data, dd_off + 1 * 8)[0]
    if import_rva == 0:
        return []               # no imports at all (unusual, but not our failure)

    # Section table → RVA-to-file-offset map.
    sec_off = opt + size_opt
    sections = []
    for i in range(num_sections):
        base = sec_off + i * 40
        va = struct.unpack_from("<I", data, base + 12)[0]
        vsize = struct.unpack_from("<I", data, base + 8)[0]
        raw_ptr = struct.unpack_from("<I", data, base + 20)[0]
        raw_size = struct.unpack_from("<I", data, base + 16)[0]
        sections.append((va, max(vsize, raw_size), raw_ptr))

    def rva_to_off(rva: int) -> int | None:
        for va, size, raw in sections:
            if va <= rva < va + size:
                return raw + (rva - va)
        return None

    def read_cstr(off: int) -> str:
        end = data.index(b"\0", off)
        return data[off:end].decode("ascii", "replace")

    names: list[str] = []
    idt = rva_to_off(import_rva)
    if idt is None:
        return []
    # IMAGE_IMPORT_DESCRIPTOR is 20 bytes; Name (RVA) is at +12. Zero entry ends it.
    i = 0
    while True:
        base = idt + i * 20
        if base + 20 > len(data):
            break
        name_rva = struct.unpack_from("<I", data, base + 12)[0]
        first_thunk = struct.unpack_from("<I", data, base + 16)[0]
        if name_rva == 0 and first_thunk == 0:
            break
        noff = rva_to_off(name_rva)
        if noff is not None:
            try:
                names.append(read_cstr(noff))
            except ValueError:
                pass
        i += 1
        if i > 4096:            # runaway guard on a corrupt table
            break
    return names


def _is_ffmpeg_dll(name: str) -> bool:
    low = name.lower()
    return low.endswith(".dll") and any(low.startswith(p) for p in _FFMPEG_PREFIXES)


def verify_av(av_dir: str, libs_dir: str) -> dict:
    """Verify every FFmpeg DLL imported by any `av/*.pyd` in `av_dir` exists as a
    file in `libs_dir`. Returns a report dict: {pyds, required, present, missing,
    errors}. `missing` non-empty ⇒ the frozen `import av` will fail."""
    pyds = sorted(glob.glob(os.path.join(av_dir, "**", "*.pyd"), recursive=True))
    have = set()
    if os.path.isdir(libs_dir):
        have = {n.lower() for n in os.listdir(libs_dir)}
    required: dict[str, list[str]] = {}   # dll -> [pyds that import it]
    errors: list[str] = []
    for pyd in pyds:
        try:
            for dll in pe_imported_dlls(pyd):
                if _is_ffmpeg_dll(dll):
                    required.setdefault(dll, []).append(os.path.basename(pyd))
        except Exception as e:
            errors.append(f"{os.path.basename(pyd)}: {type(e).__name__}: {e}")
    missing = sorted(d for d in required if d.lower() not in have)
    present = sorted(d for d in required if d.lower() in have)
    return {
        "pyds": [os.path.basename(p) for p in pyds],
        "required": sorted(required),
        "required_by": required,          # dll -> [pyds importing it]
        "present": present,
        "missing": missing,
        "errors": errors,
        "libs_dir": libs_dir,
        "libs_exists": os.path.isdir(libs_dir),
    }


def _find_source_av() -> tuple[str | None, str | None]:
    """The av package that PyInstaller will BUNDLE: `.deps/av` wins if present
    (the spec puts `.deps` first via pathex), else the build-env's importable av.
    Returns (av_dir, libs_dir) or (None, None) for a lean build with no PyAV."""
    deps_av = os.path.join(REPO, ".deps", "av")
    if glob.glob(os.path.join(deps_av, "_core*.pyd")):
        return deps_av, os.path.join(REPO, ".deps", "av.libs")
    try:
        import av  # noqa: F401
        d = os.path.dirname(av.__file__)
        return d, os.path.join(os.path.dirname(d), "av.libs")
    except Exception:
        return None, None


def _find_internal_av(root: str) -> tuple[str | None, str | None]:
    """Locate the bundled av package + av.libs anywhere under a PyInstaller dist
    dir (onedir puts them under `_internal/`; robust to layout changes)."""
    cores = glob.glob(os.path.join(root, "**", "av", "_core*.pyd"), recursive=True)
    if not cores:
        return None, None
    av_dir = os.path.dirname(cores[0])
    # av.libs is a sibling of the av package dir.
    libs = os.path.join(os.path.dirname(av_dir), "av.libs")
    if not os.path.isdir(libs):
        found = glob.glob(os.path.join(root, "**", "av.libs"), recursive=True)
        if found:
            libs = found[0]
    return av_dir, libs


def _report(rep: dict, label: str) -> int:
    print(f"[av-dll-check] {label}")
    print(f"[av-dll-check]   av.libs: {rep['libs_dir']}"
          + ("" if rep["libs_exists"] else "  (MISSING DIR)"))
    print(f"[av-dll-check]   {len(rep['pyds'])} .pyd, "
          f"{len(rep['required'])} FFmpeg DLL imports, "
          f"{len(rep['present'])} present, {len(rep['missing'])} missing")
    for e in rep["errors"]:
        print(f"[av-dll-check]   ! could not parse {e}")
    if rep["missing"]:
        print("\n[av-dll-check] ERROR (TICKET-176): av/_core.pyd imports FFmpeg DLLs that are\n"
              "               NOT in av.libs. The bundled `import av` WILL fail at runtime with\n"
              "               \"DLL load failed while importing _core\", silently disabling\n"
              "               generate-by-ear / sync-by-listening / wrong-lyrics-reject:")
        for d in rep["missing"]:
            by = ", ".join(sorted(set(rep.get("required_by", {}).get(d, []))))
            print(f"                   MISSING  {d}   (imported by {by})")
        print("\n  This is a stale-`.deps` vs collected-av.libs SKEW. Rebuild `.deps` clean so\n"
              "  its av/_core.pyd and av.libs come from ONE av build:\n"
              "      rmdir /s /q .deps  &&  pip install --target .deps -r requirements-deps.txt\n")
        return 1
    if not rep["required"]:
        print("[av-dll-check]   no FFmpeg-linked .pyd found - lean build / no PyAV (ok).")
    else:
        print("[av-dll-check] OK - every FFmpeg DLL av/_core.pyd imports is present in av.libs.")
    return 0


def main(argv: list[str]) -> int:
    mode = "source"
    internal_dir = None
    args = argv[1:]
    if "--internal" in args:
        mode = "internal"
        try:
            internal_dir = args[args.index("--internal") + 1]
        except Exception:
            print("[av-dll-check] --internal needs a directory argument")
            return 2
    elif "--source" in args:
        mode = "source"
    elif args and not args[0].startswith("-"):
        mode, internal_dir = "internal", args[0]

    if mode == "internal":
        if not internal_dir or not os.path.isdir(internal_dir):
            print(f"[av-dll-check] dist dir not found: {internal_dir!r}")
            return 2
        av_dir, libs = _find_internal_av(internal_dir)
        if not av_dir:
            print(f"[av-dll-check] no bundled PyAV under {internal_dir} — lean build (ok).")
            return 0
        return _report(verify_av(av_dir, libs), f"post-build bundle: {av_dir}")

    # source / pre-build
    av_dir, libs = _find_source_av()
    if not av_dir:
        print("[av-dll-check] no PyAV in .deps or the build env — lean build, nothing to check (ok).")
        return 0
    if not glob.glob(os.path.join(av_dir, "_core*.pyd")):
        print(f"[av-dll-check] {av_dir} has no _core*.pyd — not a delvewheel PyAV; skipping (ok).")
        return 0
    return _report(verify_av(av_dir, libs), f"pre-build source: {av_dir}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))

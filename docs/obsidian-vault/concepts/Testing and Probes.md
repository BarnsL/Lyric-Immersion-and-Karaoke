# Testing and Probes

- `tests/` (pytest; CI on Linux under xvfb): `test_concert_audio.py` (synthetic,
  hermetic), `test_concert_sync.py` (real `Overlay` methods, stubbed side effects),
  media-source policy, playlist import.
- `scripts/probe_*.py`: ast-extract functions from `main.py` and exec against hostile
  stubs (no Tk app). `probe_concert.py`, `probe_tune_docs.py` run in CI.
- `scripts/eval_concert_mc.py`: before/after evaluation on permissively-licensed audio.

Practice: prove a test fails on the old code before trusting it passes on the new.

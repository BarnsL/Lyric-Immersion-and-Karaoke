"""Single source of truth for the app version.

Used by the local API (`api.py`), the in-app updater (`updater.py`), and read by
the build scripts. Keep in sync with the MSIX `Version` and the Inno `AppVer`
when cutting a release (the last MSIX digit must stay 0, e.g. 1.0.1.0).
"""
__version__ = "1.0.15"

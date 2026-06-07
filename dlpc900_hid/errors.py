"""Exceptions for the dlpc900_hid package."""


class DMDError(Exception):
    """Base exception for all DMD-related errors."""
    pass


# Backwards-compatible alias matching the upstream dlpyc900 naming.
DMDerror = DMDError

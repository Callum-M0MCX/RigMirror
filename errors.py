"""Exceptions shared by CAT transport and radio drivers."""


class CATError(RuntimeError):
    """Base exception for CAT transport failures."""


class CATTimeoutError(CATError):
    """Raised when a complete CAT response is not received in time."""

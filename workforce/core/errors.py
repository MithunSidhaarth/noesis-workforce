"""Exceptions shared across layers (agents, gateway, runtime)."""


class PermanentError(Exception):
    """Non-retryable task failure (bad input, policy block)."""

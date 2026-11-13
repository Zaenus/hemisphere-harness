"""Exceptions.

The two hemisphere errors are handled asymmetrically, on purpose. A failing left
hemisphere is survivable: the harness hands the decision back to the right. A
failing right hemisphere ends the run: there is no agent without the Master.
"""


class HarnessError(Exception):
    """Base class for everything this package raises."""


class ConfigError(HarnessError):
    """Configuration is missing or inconsistent."""


class LeftHemisphereError(HarnessError):
    """The left hemisphere failed or answered outside what it was asked.

    Covers API failures, timeouts and malformed responses. The harness catches
    this and escalates the decision to the right.
    """


class RightHemisphereError(HarnessError):
    """The right hemisphere failed or returned something unusable. Fatal to a run."""

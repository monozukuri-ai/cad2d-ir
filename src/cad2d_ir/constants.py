"""Package-wide format constants."""

# The version of the IR document format, separate from the package version.
# Bump the minor part when a change alters what the writers read from a
# document: a field or a ``metadata`` key a writer reads, or what it means
# (CONTRIBUTING.md, "IR version"). Consumers that take IR documents from
# outside may accept only the current and the previous version.
CURRENT_IR_VERSION = "0.3.0"

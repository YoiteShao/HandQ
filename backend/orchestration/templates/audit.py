"""Compatibility stub — re-exports the audit template build function."""
from . import audit as _audit_ns
build = _audit_ns.build
DEFAULT_SCANNERS = _audit_ns.DEFAULT_SCANNERS

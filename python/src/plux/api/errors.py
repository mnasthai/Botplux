"""Public errors. Business rejection is an Outcome."""
class PluxError(Exception):
    code = "plux_error"

class ConfigurationError(PluxError):
    code = "configuration_error"

class ConflictError(PluxError):
    code = "conflict"

class ResourceMissing(PluxError):
    code = "resource_missing"

class ResourceNotReady(PluxError):
    code = "resource_not_ready"

class UnsupportedCapability(PluxError):
    code = "unsupported_capability"

class InvalidScope(PluxError):
    code = "invalid_scope"

class InfrastructureError(PluxError):
    code = "infrastructure_error"

class UncertainResult(PluxError):
    code = "uncertain_result"

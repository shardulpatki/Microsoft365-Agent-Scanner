from enum import StrEnum


class Transport(StrEnum):
    streamable_http = "streamable_http"
    custom_connector = "custom_connector"
    copilot_connector = "copilot_connector"


class AuthType(StrEnum):
    oauth2_dcr = "oauth2_dcr"
    oauth2_static = "oauth2_static"
    api_key = "api_key"
    managed = "managed"
    none = "none"


class Severity(StrEnum):
    info = "info"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class AgentPath(StrEnum):
    copilot_studio = "copilot_studio"
    declarative = "declarative"


class WiredVia(StrEnum):
    native_mcp_tool = "native_mcp_tool"
    custom_connector = "custom_connector"


class ScanStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"

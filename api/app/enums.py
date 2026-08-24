from enum import Enum


class UITheme(str, Enum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class ProjectRole(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class RunStatus(str, Enum):
    PENDING_UPLOAD = "PENDING_UPLOAD"
    UPLOADED = "UPLOADED"
    INGESTING = "INGESTING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class ResourceType(str, Enum):
    SMB_SHARE = "smb_share"
    NFS_SHARE = "nfs_share"


class AccessLevel(str, Enum):
    UNKNOWN = "unknown"
    NO_ACCESS = "no_access"
    LIST_ONLY = "list_only"
    READABLE = "readable"


class ErrorSeverity(str, Enum):
    WARN = "warn"
    ERROR = "error"

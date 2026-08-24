from __future__ import annotations

from app.enums import ResourceType

_RESOURCE_TO_SHARE = {
    ResourceType.SMB_SHARE.value: "smb",
    ResourceType.NFS_SHARE.value: "nfs",
    ResourceType.SHAREPOINT_LIBRARY.value: "sharepoint",
}


def share_type_from_resource_type(value: ResourceType | str | None) -> str:
    if isinstance(value, ResourceType):
        return _RESOURCE_TO_SHARE.get(value.value, "unknown")
    if isinstance(value, str):
        return _RESOURCE_TO_SHARE.get(value.strip().lower(), "unknown")
    return "unknown"

from app.enums import ResourceType
from app.share_types import share_type_from_resource_type


def test_share_type_from_resource_type_handles_known_values() -> None:
    assert share_type_from_resource_type(ResourceType.SMB_SHARE) == "smb"
    assert share_type_from_resource_type(ResourceType.NFS_SHARE) == "nfs"
    assert share_type_from_resource_type("smb_share") == "smb"
    assert share_type_from_resource_type("nfs_share") == "nfs"
    assert share_type_from_resource_type(ResourceType.SHAREPOINT_LIBRARY) == "sharepoint"
    assert share_type_from_resource_type("sharepoint_library") == "sharepoint"


def test_share_type_from_resource_type_does_not_mislabel_unknown_providers_as_smb() -> None:
    assert share_type_from_resource_type("unknown") == "unknown"
    assert share_type_from_resource_type(None) == "unknown"

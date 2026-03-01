from app.enums import ResourceType
from app.share_types import share_type_from_resource_type


def test_share_type_from_resource_type_handles_known_values() -> None:
    assert share_type_from_resource_type(ResourceType.SMB_SHARE) == "smb"
    assert share_type_from_resource_type(ResourceType.NFS_SHARE) == "nfs"
    assert share_type_from_resource_type("smb_share") == "smb"
    assert share_type_from_resource_type("nfs_share") == "nfs"


def test_share_type_from_resource_type_defaults_to_smb() -> None:
    assert share_type_from_resource_type("unknown") == "smb"
    assert share_type_from_resource_type(None) == "smb"

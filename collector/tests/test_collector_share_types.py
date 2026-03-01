import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_collector_module():
    module_path = Path(__file__).resolve().parents[1] / "smbguard_collector.py"
    spec = importlib.util.spec_from_file_location("smbguard_collector", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_selected_share_types_supports_both_and_defaults() -> None:
    collector = _load_collector_module()

    assert collector._selected_share_types("smb") == {"smb"}
    assert collector._selected_share_types("nfs") == {"nfs"}
    assert collector._selected_share_types("both") == {"smb", "nfs"}
    assert collector._selected_share_types("unknown") == {"smb"}


def test_parse_showmount_exports_extracts_unique_paths() -> None:
    collector = _load_collector_module()
    output = """
Exports list on host:
/srv/public 10.0.0.0/24
/srv/private 10.0.0.5
/srv/public 10.0.0.10
not-a-path value
"""

    exports = collector._parse_showmount_exports(output)

    assert exports == ["/srv/public", "/srv/private"]


def test_scan_host_dispatch_runs_only_selected_share_scanners(monkeypatch) -> None:
    collector = _load_collector_module()
    calls: list[str] = []

    monkeypatch.setattr(
        collector,
        "scan_host_smb",
        lambda *_args, **_kwargs: calls.append("smb") or True,
    )
    monkeypatch.setattr(
        collector,
        "scan_host_nfs",
        lambda *_args, **_kwargs: calls.append("nfs") or False,
    )

    args = SimpleNamespace(share_types="both")
    ok = collector.scan_host("10.0.0.5", args, "run-1", SimpleNamespace(emit=lambda *_: None), collector.Stats(), object())

    assert ok is True
    assert calls == ["smb", "nfs"]

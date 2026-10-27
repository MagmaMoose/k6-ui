import asyncio
from pathlib import Path

import pytest

from k6_ui.archive import ArchiveError, archive


def test_archiver_passes_only_defined_variables_and_never_inherits_secrets(tmp_path, monkeypatch):
    source = tmp_path / "smoke.js"; source.write_text("export default function() {}")
    captured = {}

    async def spawn(*args, **kwargs):
        captured.update(args=args, **kwargs)
        Path(args[args.index("-O") + 1]).write_bytes(b"archive")
        class Process:
            returncode = 0
            async def wait(self): return 0
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "must-not-be-inherited")
    assert asyncio.run(archive(tmp_path, "smoke.js", {"VUS": "1000", "RAMP": "90s"})) == b"archive"
    assert "--include-system-env-vars=false" in captured["args"]
    assert "VUS=1000" in captured["args"]
    assert "AZURE_CLIENT_SECRET" not in captured["env"]
    assert captured["cwd"] == tmp_path


def test_archiver_refuses_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside.js"; outside.write_text("export default function() {}")
    (tmp_path / "escape.js").symlink_to(outside)
    with pytest.raises(ArchiveError): asyncio.run(archive(tmp_path, "escape.js", {}))

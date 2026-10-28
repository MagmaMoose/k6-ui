import asyncio
import os
import tempfile
from pathlib import Path

import httpx

MAX_ARCHIVE_BYTES = 700000


class ArchiveError(Exception):
    pass


async def request_archive(_root: Path, script: str, variables: dict[str, str], bundle: bool = False, transport=None) -> bytes:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=70, trust_env=False) as client:
            response = await client.post("http://127.0.0.1:8081/archive", json={"script": script, "variables": variables, "bundle": bundle})  # DevSkim: ignore DS162092 - production sidecar stays on pod loopback
    except httpx.HTTPError as err:
        raise ArchiveError("The script archiver is unavailable.") from err
    if response.status_code != 200:
        raise ArchiveError("The defined script could not be bundled. Check its imports and configuration.")
    if not response.content or len(response.content) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("Script archive exceeds the 700000-byte ConfigMap limit.")
    return response.content


async def run(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    try:
        process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except OSError as err:
        raise ArchiveError("The k6 archiver could not start.") from err
    try:
        await asyncio.wait_for(process.wait(), timeout=60)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ArchiveError("The defined script could not be bundled. Check its imports and configuration.")


async def archive(root: Path, script: str, variables: dict[str, str], bundle: bool = False) -> bytes:
    root = root.resolve()
    source = (root / script).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ArchiveError("The defined script is unavailable. Check the script source sync.")
    with tempfile.TemporaryDirectory(prefix="k6-ui-") as work:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": work, "TMPDIR": work, "K6_NO_USAGE_REPORT": "true"}
        if bundle:
            # Suites written for a bundler import modules without file extensions, which k6 cannot resolve.
            entry = Path(work) / f"{source.stem}.js"
            await run(["esbuild", str(source), "--bundle", "--format=esm", "--platform=neutral", "--external:k6", "--external:k6/*",
                       "--log-level=error", f"--outfile={entry}"], root, env)
            source = entry
        output = Path(work) / "archive.tar"
        args = ["k6", "archive", "--quiet", "--include-system-env-vars=false"]
        for key, value in variables.items():
            args.extend(["-e", f"{key}={value}"])
        args.extend(["-O", str(output), str(source)])
        await run(args, root, env)
        if not output.is_file() or output.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ArchiveError("Script archive exceeds the 700000-byte ConfigMap limit.")
        return output.read_bytes()

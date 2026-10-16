"""Owned subprocesses with disk-backed output and bounded cleanup."""

from __future__ import annotations
import asyncio
import os
import signal
import uuid
from pathlib import Path


class Jobs:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.directory = self.project / ".weyra" / "jobs"
        if not self.directory.resolve().is_relative_to(self.project):
            raise PermissionError("Job directory escapes project root")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jobs = {}

    async def start(self, command, owner, cwd=None):
        cwd = Path(cwd or self.project).resolve()
        if not cwd.is_relative_to(self.project):
            raise PermissionError("Job cwd escapes project root")
        job_id = uuid.uuid4().hex
        output = self.directory / (job_id + ".log")
        handle = output.open("wb")
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd or self.project,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            handle.close()
        self.jobs[job_id] = {"process": process, "owner": owner, "output": output}
        return self.status(job_id)

    def status(self, job_id):
        job = self.jobs[job_id]
        return {
            "job_id": job_id,
            "owner": job["owner"],
            "returncode": job["process"].returncode,
            "running": job["process"].returncode is None,
            "output_path": str(job["output"]),
        }

    async def wait(self, job_id, timeout=30):
        try:
            await asyncio.wait_for(self.jobs[job_id]["process"].wait(), min(max(timeout, 0), 300))
        except asyncio.TimeoutError:
            pass
        return self.status(job_id)

    async def cancel(self, job_id):
        process = self.jobs[job_id]["process"]
        # The shell may have exited while descendants still hold its process group.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            await asyncio.wait_for(process.wait(), 2)
        return self.status(job_id)

    def transfer(self, job_id, owner, new_owner=None):
        job = self.jobs[job_id]
        if new_owner is not None and job["owner"] != owner:
            raise PermissionError("Job belongs to another turn")
        job["owner"] = new_owner if new_owner is not None else owner
        return self.status(job_id)

    async def cleanup_owner(self, owner):
        await asyncio.gather(
            *(self.cancel(key) for key, job in self.jobs.items() if job["owner"] == owner)
        )

    async def close(self):
        await asyncio.gather(*(self.cancel(key) for key in self.jobs))

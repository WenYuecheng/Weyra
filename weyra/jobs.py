"""受控子进程作业调度器（Owned Jobs Supervision）与磁盘日志持久化。

架构定位与设计哲学:
    本模块为 Weyra 运行时的异步子进程提供企业级监管。
    在 AI Agent 执行编译、测试、长时间构建或后台守护进程时，传统的 popen 存在进程组失控、
    孤儿僵尸进程泄露、输出阻塞导致死锁（Pipe Buffer Deadlock）等重大稳定性隐患。
    本模块借鉴现代操作系统与工业级 Agent 框架的进程治理模型，通过独立会话组硬隔离、
    磁盘文件落盘重定向、所有权归属转移与两阶段超时信号升级，确保宿主与子进程的确定性解耦。

开源框架借鉴与设计原则:
    1. DeepSeek-Harness (packages/jobs & packages/subprocess):
       - 借鉴其所有权生命周期模型（Ownership Lifecycle）：每个作业严格归属于特定轮次（ctx.id）
         或持久会话（session:xxx），禁止跨所有者非法操作；支持任务平滑移交（transfer）。
       - 磁盘缓冲重定向（Disk-backed Output）：子进程的 stdout 与 stderr 直接重定向到磁盘日志文件
         （.weyra/jobs/{job_id}.log），避免了内存缓冲区（Pipe Buffer）爆满导致子进程挂起的系统死锁。
       - 会话组硬隔离（Session Group Isolation）：创建子进程时强制启用 `start_new_session=True`，
         将子进程及其所有衍生孙进程隔离在专属 POSIX 进程组中。
       - 两阶段级联取消机制（SIGTERM -> SIGKILL Escalation）：执行 cancel 时先发送 SIGTERM 优雅退出；
         等待 2 秒若仍未终止，立即向整个进程组广播 SIGKILL，彻底根绝孤儿后台进程。
       - 沙箱越界防御（Jail Boundary Check）：严格校验作业执行目录与作业根目录必须在 project 边界内，
         防御路径穿越（Directory Traversal Escape）。
"""

from __future__ import annotations
import asyncio
import os
import signal
import uuid
from pathlib import Path


class Jobs:
    """受控作业监督管理器。

    管理作业创建、状态追踪、异步等待、双阶段信号熔断、所有权流转与批量清场。
    """

    def __init__(self, project):
        """初始化作业管理器。

        Args:
            project: 项目根目录绝对路径。
        """
        self.project = Path(project).resolve()
        self.directory = self.project / ".weyra" / "jobs"
        if not self.directory.resolve().is_relative_to(self.project):
            raise PermissionError("Job directory escapes project root")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jobs = {}

    async def start(self, command, owner, cwd=None):
        """启动一个新的后台受控子进程作业。

        Args:
            command: 待执行的 Shell 命令。
            owner: 作业所有者标识（如当前轮次 ID）。
            cwd: 运行工作目录（必须在项目根目录之内）。

        Returns:
            dict: 作业元数据状态字典。
        """
        cwd = Path(cwd or self.project).resolve()
        if not cwd.is_relative_to(self.project):
            raise PermissionError("Job cwd escapes project root")
        job_id = uuid.uuid4().hex
        output = self.directory / (job_id + ".log")
        handle = output.open("wb")
        try:
            # 独立会话组与输出文件句柄绑定，避免管道阻塞
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
        """获取指定作业的当前运行状态与日志文件路径。"""
        job = self.jobs[job_id]
        return {
            "job_id": job_id,
            "owner": job["owner"],
            "returncode": job["process"].returncode,
            "running": job["process"].returncode is None,
            "output_path": str(job["output"]),
        }

    async def wait(self, job_id, timeout=30):
        """等待作业运行完成或直至超时（最长 300 秒防护上限）。"""
        try:
            await asyncio.wait_for(self.jobs[job_id]["process"].wait(), min(max(timeout, 0), 300))
        except asyncio.TimeoutError:
            pass
        return self.status(job_id)

    async def cancel(self, job_id):
        """执行两阶段强力信号熔断机制，安全终止进程组。

        步骤:
            1. 向整个进程组（os.killpg）广播 SIGTERM 信号，给予进程优雅保存状态并退出的机会。
            2. 异步等待 2 秒超时；若进程组内仍有残留进程未退出，强制发送 SIGKILL 强杀。
        """
        process = self.jobs[job_id]["process"]
        # Shell 可能已经退出，但其衍生的子进程仍霸占进程组
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
        """安全流转作业所有权。"""
        job = self.jobs[job_id]
        if new_owner is not None and job["owner"] != owner:
            raise PermissionError("Job belongs to another turn")
        job["owner"] = new_owner if new_owner is not None else owner
        return self.status(job_id)

    async def cleanup_owner(self, owner):
        """并发取消并清空特定所有者的所有托管作业（用于轮次中断时的资源清理屏障）。"""
        results = await asyncio.gather(
            *(self.cancel(key) for key, job in self.jobs.items() if job["owner"] == owner),
            return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError('Job cleanup failed after draining owned jobs')

    async def close(self):
        """排干并销毁系统内所有后台作业（应用程序退出时调用）。"""
        results = await asyncio.gather(*(self.cancel(key) for key in self.jobs), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError('Job cleanup failed after draining all jobs')


"""提示词模板、环境动态快照与 DSH 检查点压缩规范 (Prompt Templates & DSH Compaction Specs)。

【架构定位】
本模块负责系统动态提示词组件的管理。
包含用于会话上下文压缩的 8 分段结构化检查点提取指令（Pinned to DeepSeek-Harness 上游稳定 Commit: d743267388641bc76f17c45ce8b4c231aed1d32c），
以及为每个执行轮次动态采集当前项目 Git 分支、文件修改状态与观测时间戳的环境快照函数。

【开源与工程设计借鉴】
1. 借鉴 DeepSeek-Harness (`packages/compaction/`) 的 8 分段检查点结构 (DSH 8-Section Schema)：
   - 包含：主要诉求（Primary Request）、关键技术概念（Key Technical Concepts）、涉及文件与代码（Files and Code）、
     错误与修复（Errors and Fixes）、待办任务（Pending Jobs）、当前工作（Current Work）、下一步动作（Next Step）、
     关键上下文（Critical Context）；
   - 通过显式格式约束与空节标注 `(none)`，杜绝大模型在压缩时遗漏核心约束或丢失代码路径；
2. 深度契合 Pi Agent 与 Anthropic KV Cache 严格前缀缓存不变性：
   - 将工作区环境动态信息（Git 状态、时间戳等）封装为独立观察消息注入会话历史，
   - 严禁将其拼接进系统顶层静态提示词（System Prompt），杜绝破坏静态前缀导致的大模型 KV 缓存失效与延迟激增。
"""

from weyra.core.compaction_prompt import (
    SUMMARY_OPEN_TAG, SUMMARY_CLOSE_TAG, COMPACTION_INSTRUCTION, CHECKPOINT_PREAMBLE,
)  # noqa: F401


async def environment_snapshot(project):
    """异步采集项目 Git 工作区状态与观测时间戳。

    【架构定位】
    为每个执行轮次动态捕获工作区外部事实（当前工作区路径、UTC 观测时间与 Git 分支/修改状态），
    作为独立的轮次观察消息注入会话历史。

    【开源设计思想与架构血统】
    深度契合 Pi Agent 与 Anthropic KV Cache 严格前缀缓存不变性（Prefix Prompt Caching）。
    动态变化的工作区事实绝不拼接进入顶层系统提示词（System Prompt），
    杜绝频繁变化导致大模型 KV Cache 击穿。
    同时借鉴 DeepSeek-Harness 的环境快照采集超时熔断设计（2 秒超时截断与 8192 字节上限）。

    【核心不变性与安全约束】
    1. 非阻塞超时保护：限制 Git 进程最多执行 2 秒，超时直接熔断置为 "unavailable"；
    2. 无锁与去监控：通过 `--no-optional-locks` 与 `-c core.fsmonitor=false` 避免阻塞或与宿主 IDE 产生锁竞争；
    3. 异常进程清理：`finally` 块强制对未终止的子进程执行 kill，彻底杜绝孤儿僵尸进程。

    Args:
        project: 待采集的项目根目录绝对路径或 Path 对象。

    Returns:
        dict[str, str]: 包含 `project`, `observed_at`, `git_status` 的环境事实字典。
    """
    import asyncio
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    result = {
        "project": str(Path(project).resolve()),
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "status",
        "--short",
        "--branch",
        "--",
        ".",
    ]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=project,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}},
        )
        output, _ = await asyncio.wait_for(process.communicate(), 2)
        if process.returncode == 0:
            result["git_status"] = output.decode("utf-8", "replace")[:8192]
    except (FileNotFoundError, asyncio.TimeoutError):
        result["git_status"] = "unavailable"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
    return result

__all__ = ["SUMMARY_OPEN_TAG", "SUMMARY_CLOSE_TAG", "COMPACTION_INSTRUCTION", "CHECKPOINT_PREAMBLE", "environment_snapshot"]

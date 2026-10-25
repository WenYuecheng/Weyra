# 贡献指南

Weyra 欢迎围绕持续任务、执行治理、上下文与技能验证的改进。贡献应保持模型协议、执行核心、领域装配和 UI 的清晰边界。

## 开发环境

在本仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,tui]'
```

## 验证

```bash
python -m pytest -q
ruff check .
python -m build
python scripts/verify_package.py
python -m examples.sdk_service
```

修改行为时补充能复现问题的回归用例；仅通过几个模块的测试不能替代发布前的完整测试。离线演示可使用 `weyra demo --scenario analysis --offline --project /tmp/weyra-demo`。

## 代码约定

- 4 空格缩进；公开接口提供类型与职责说明，注释解释约束和原因。
- 模型调用经统一运行时进行预算预留与用量记录；工具调用经统一权限与调度入口。
- 保持工具调用与结果配对，取消后回收所拥有的进程与协程。
- 会话历史以追加事件保存；修改投影、分支或版本时使用事务并保留恢复依据。
- 维护稳定提示词基线，动态事实使用明确来源，按变更更新容量检查。
- 显式说明行为变更、兼容性和验证结果。

## 文档与来源

公共手册位于 `docs/`，研究映射位于 `paper/`。代码示例必须对应实际签名并运行验证。实验结果注明任务范围、运行协议、模型路由、预算与费用依据；摘要放在 `docs/evidence/`，公开前移除凭据和本机路径。

借鉴机制时标注来源；直接改编的代码保留相应版权与许可。区分上游已有方法与本项目的集成实现，按原始论文版本引用。

提交前确认完整测试与相关交付检查通过，未包含 `.env`、个人 `weyra.toml` 或私有运行状态。可使用 `fix(scope): ...`、`feat(scope): ...`、`docs(scope): ...` 等提交格式。

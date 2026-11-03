# Weyra 0.1.3 架构治理交付

日期：2026-10-20。

## 使用与兼容

普通安装保留 `weyra`、`weyra eval --offline`、两个演示和完整TUI资源。
历史评测的10个实现及6份数据已迁至 `benchmarks/`，随源码包提供。
旧 `weyra.core_*`、`weyra.finish_*`、`weyra.final_*` 导入在源码环境映射到同一个实现；普通wheel会明确提示使用源码仓库或源码包。

源码环境运行：

```sh
python -m pip install -e '.[dev]'
python -m benchmarks.final_delivery_eval --help
python -m scripts.reports.finish_report --help
python examples/verify.py
```

历史报告工具移至 `scripts/reports/`，串行评测启动工具移至 `scripts/run_finish.py`。
`requirements.txt` 通过 `-e .` 复用项目依赖。新增历史结果使用 `.benchmarks/results/`；付费运行仍要求显式输出和账本目录。

## 已完成的治理

- 普通wheel移除历史评测实现与历史数据，保留日常20场景数据和必要提示模板。
- 新源码指纹覆盖嵌套运行模块、TUI资源、历史评测实现与数据；新协议版本区分新指纹布局。
- 打包验证改用自动清理的临时环境，摘要写入 `.benchmarks/results/engineering/`。
- 清理4个已核实的验证虚拟环境与1个旧构建备份，共约75MiB。
- 清理前 `.weyra` 为1090212KiB，清理后1013220KiB。其余空间包含保留的数据库与实验依据，不设置人为体积目标。
- 3323个受保护文件已校验指纹：数据库、历史冻结源、协议、结果及既有Wiki JSON保持不变。

## 验证与产物

全量离线回归509项测试、5项子测试通过；静态检查、演示及20场景离线评测通过。
wheel压缩体积由375686字节降至175603字节，解压体积由2989179字节降至656599字节。
最终安装包、源码包及详细验证清单见 `GOVERNANCE-0.1.3-VERIFICATION.json`。
安装验证覆盖仓库外运行、配置检查、历史入口指引、源码数据加载和离线报告生成。

本轮不调用收费模型，不提交或推送Git变更，不改写历史成绩，不拆分运行内核。

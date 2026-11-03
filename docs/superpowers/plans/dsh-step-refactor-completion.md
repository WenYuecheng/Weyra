# DSH 后续架构改造执行记录

- [x] 冻结原始源码指纹并保留未提交工作。
- [x] PreparedStep / PreparedModelCall 与统一清理。
- [x] 注册句柄、条件集合、公共工具管道与调用方迁移。
- [x] 独立投影、窄读取、schema v2 与缓存恢复。
- [x] GoalRef CAS、resume/restart 与服务/模型/CLI 接入。
- [x] 专项、完整回归、构建、隔离安装、UI/服务/迁移冒烟。
- [x] 读取对照、文档、简历及证据账本同步。

866 项完整回归通过、3 项跳过、5 subtests 通过。详见 docs/DSH_STEP_ACCEPTANCE.md。

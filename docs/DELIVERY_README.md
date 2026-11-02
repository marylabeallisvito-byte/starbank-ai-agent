# 星河银行智能体 · 本机评审包

此包使用虚构账户与仿真资金。默认关闭外部模型和数据接口；没有配置外部服务时，不冒充已接入。它不是生产银行系统。

## 环境与启动

本轮构建来自 Windows / Python 3.13。建议用相同 Python 与系统环境创建独立虚拟环境，再执行：

1. `python -m pip install -r requirements.lock.txt`。
2. `python -m scripts.init_banking_demo`。仅在没有 `.env` 时创建新的本机加密密钥与工作人员凭据，不输出令牌内容，也不覆盖已有配置。
3. `python -m scripts.launch_banking --check`。检查发布文件完整性与必要配置。
4. `python -m scripts.launch_banking --port 5173`。
5. 在 Chrome/Edge 打开 `http://localhost:5173/banking`。

前端已经构建，无需安装 Node 或执行开发服务器。通行密钥验证需使用配置一致的 localhost 来源，由本人操作设备弹窗。更换端口时请同步检查 WebAuthn 的来源配置。默认不会用模拟验证码代替设备验证。

工作人员登录可读取初始化生成的 `backend/data/private/local-reviewer.token`。请由演示管理员保管，不交给普通参与者。Windows 文件访问权限应按部署环境限制。

需要外部模型时，在本机配置兼容服务的 `LLM_API_KEY`、`LLM_API_BASE`、`LLM_MODEL` 并显式启用 `BANKING_LLM_ENABLED=1`。Plaid 当前仅支持 Sandbox，属于可选只读数据。所有密钥均不随提交包提供。

## 发布完整性与范围

`banking-release.json` 保存文件 SHA-256；ZIP 旁有单独校验文件。文件检查可发现发布后修改或缺失，不是可信机构签名，也不能抵御同时篡改文件和清单的人。

发布包按白名单收集源代码、构建文件、语料、必要报告与依赖信息。排除 `.env`、数据库、上传账单、研究记录、工作人员令牌和虚拟环境；打包时扫描本机已配置的敏感值。

`requirements.lock.txt` 是本轮环境的版本快照。跨系统安装、冷启动依赖下载和离线依赖安装未因此自动得到验证。`THIRD_PARTY_NOTICES.txt` 记录构建环境中收集到的前端依赖许可，发布前仍应复核项目原创贡献与适用许可。

## 演示与验证

先演示月度目标、相对金额修改与授权边界，再演示异常复核和用户恢复。最后可演示由本人设备确认的红色虚构交易。没有真实设备操作时，不声称完成硬件验收。

新任务挑战首次结果保存在 `backend/eval/reports/monthly_challenge_v1/first_run.json`。它是开发者编写的新任务集，不是独立人员盲测；同集修复后重测只能作为开发回归。

`python -m backend.eval.banking_release_eval` 运行规则模式回归；两条固定模糊意图可能需要模型。`--live-model` 使用本机显式配置的模型。所有测试使用隔离数据库。

新增或修改源代码后，应重建、复验并生成新发布包，不应编辑既有发布清单来掩盖版本变化。当前源码包含的验收报告以各自版本指纹和范围为准。

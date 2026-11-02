# 原创贡献与第三方组件

## 原创业务实现

本项目的主要贡献是将有界条件编译与银行任务状态连接，以版本摘要绑定用户授权，并把实际输入证据、异常复核、客户恢复和幂等事务衔接为完整流程。实现位于 `backend/banking_goal_planner.py`、`banking_missions.py`、`banking_mission_evidence.py`、`banking_execution.py` 及前端银行工作台等模块。

## 第三方软件

前端使用 React、Vite、TypeScript、Lucide、AntV G6 等组件；后端使用 FastAPI、Pydantic、WebAuthn、cryptography 和 SQLite 等。LangChain、LangGraph 与 Neo4j 服务于保留的关系调查扩展。依赖不是团队原创；版本见 `requirements.lock.txt` 与 `frontend/package-lock.json`。前端构建环境收集的许可原文见 `THIRD_PARTY_NOTICES.txt`，Python 分发包许可信息见 `docs/dependency-metadata.json`。

本次没有为宣传效果拼接无法运行的外部 Agent 框架，也不把引用开源组件本身称为核心算法创新。第三方许可的适用性须随实际分发方式复核。

## 数据与图片

银行账户、交易和理财产品是合成仿真数据。公开通知、参考汇率及 Plaid Sandbox 各有来源边界，不形成真实账户风控认证。

BANKING77 外部协议保留 PolyAI 与原作者署名、源地址和 CC BY 4.0 标记。仓库只提供选定评测记录及协议，不主张拥有原始数据权利。

封面概念图由内置图像生成工具生成，未用于表示实际界面或量化结果。提示词保存在 `docs/assets/image-generation-prompt.txt`。架构与权限图按代码确定性绘制，页面截图来自实际仿真系统。图示可编辑 SVG 随附，不包含用户金融信息。

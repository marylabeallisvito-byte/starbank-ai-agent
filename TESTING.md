# 测试与复现

## 已归档证据

基线为 2026-11-02：后端 271/271 方法、固定合成意图 24/24、发布包运行 20/20。后端报告包含源码指纹、逐项断言和模型环境。报告不能作为当前任意修改版的证明。

## 只运行回归方法

```powershell
.\.venv\Scripts\python.exe -m scripts.check_submission_regressions
```

该入口从原发布评测器读取已登记的模块集合，以独立临时数据库执行同一回归方法，不重写归档评测报告，也不需要模型密钥。源码中的测试均使用合成数据或故障注入。首次运行先完成环境初始化；测试夹具所需 XLS/XLSX/CSV 已随 `docs/examples/` 提供。

## 连同固定意图运行

```powershell
.\.venv\Scripts\python.exe -m backend.eval.banking_release_eval --live-model
```

这会联网使用本机显式配置的语义模型，并重写开发目录下的评测报告。请在**单独副本**中执行，避免改变本次提交的冻结归档。模型、服务商和网络状态可能影响结果。无 `--live-model` 时规则模式下的模糊表达不保证 24/24。

## 专项入口

```powershell
.\.venv\Scripts\python.exe -m unittest backend.tests.test_banking_lifecycle_matrix backend.tests.test_banking_feed_resilience -v
.\.venv\Scripts\python.exe -m unittest backend.tests.test_banking_semantic_safety backend.tests.test_banking_refinements -v
```

生命周期矩阵包含子场景，不把内部子场景数描述为独立用户数。签名算法夹具不是本人设备测试。

## 新题与模拟评测

月度挑战首次 21/26，修复后同集 26/26；AI 模拟专家首次 14/24，修复后同集 24/24。`first_run.json` 不覆盖。后续同集重跑只能登记为开发回归，不能改称新的未见任务。

外部 BANKING77 四类英文子集首轮 22/40，保留失败；语料协议与来源作者在原报告中。它不是完整 BANKING77 分类结果，也不是独立第三方认证。真实用户样本 0，开发者走查不产生人类满意度或效率提升结论。

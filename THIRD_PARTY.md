# 第三方来源与使用方式

## 课程指定运行时

- 仓库：[GameAI-NJUPT/OpenGuanDan](https://github.com/GameAI-NJUPT/OpenGuanDan)
- 固定版本：`52e473a5b7f5e90df47b1564416a48ec4ac5ef5b`
- 使用：课程平台、Java 动作 worker、Python 离线环境。
- 本轮未发现明确 LICENSE；下载器仅在用户执行后获取课程指定运行时，不把原始二进制或源码提交到项目 Git。
- 每个文件校验 Git blob SHA-1，并记录来源 URL 和 SHA-256。
- 未改动第三方原文件。适配器修避上游固定 deck_data 初始化后无法正确重洗牌的问题；没有更改其结算规则。

## 已发布 AI 对照

- 仓库：[welkin03/guandan-ai](https://github.com/welkin03/guandan-ai)
- 固定版本：`3ab3523abbca2c703d5c0e421b85b23214292e91`
- 许可证：[MIT](https://github.com/welkin03/guandan-ai/blob/3ab3523abbca2c703d5c0e421b85b23214292e91/LICENSE)，Copyright (c) 2026 welkin03。
- 使用：[heuristic_policy](https://github.com/welkin03/guandan-ai/blob/3ab3523abbca2c703d5c0e421b85b23214292e91/src/guandan/policy.py)、手牌估价和相关类型。
- 原始文件、完整 LICENSE 与哈希清单保存在 `.runtime/welkin-guandan-ai/`；未在 Git 中重新发布。
- 适配更改：平台合法动作替代上游根动作生成；牌编码映射；对其他手牌仅提供张数；禁用未被基础 heuristic_policy 调用的额外全信息 guards；进贡/还贡使用本项目策略；缓存相同手牌估价。
- 不宣称复现其未公开研究栈、神经权重或原作者发布的成绩。

Streamlit、websockets、pandas、Matplotlib 和 pytest 按包管理器各自许可证使用。项目没有包含 DanZero+ 或其他授权不明确项目的实现代码。


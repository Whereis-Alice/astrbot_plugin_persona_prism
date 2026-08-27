# NOTICE

本插件（`astrbot_plugin_persona_prism` / 人格棱镜）是一个独立实现的衍生作品，
其产品创意、指令语义与部分实现思路参考了以下上游项目：

## astrbot_plugin_portrayal

- 作者：Zhalslar
- 仓库：https://github.com/Zhalslar/astrbot_plugin_portrayal
- 许可：MIT License

本插件复用了上游的以下概念性设计：

1. "抓取群聊历史消息 → 交给 LLM 生成群友人格画像"的整体玩法；
2. 基于 OneBot `get_group_msg_history` 的历史消息反向翻页取数思路；
3. 多种画像风格（画像 / 赞赏 / 锐评 / 人格克隆 / 姻缘）的产品分类。

本插件的代码、数据模型、存储层、提示词、卡片渲染与 WebUI 均为重写实现，
并对上游已知问题做了修复（详见 README 的"相对上游的改进"一节）。

在此向 Zhalslar 及上游项目致谢。

---

其他第三方依赖：

- PyYAML — BSD 3-Clause License
- AstrBot 运行时（宿主提供）— AGPL-3.0-or-later
- Jinja2（经 AstrBot 宿主提供，用于 HTML 模板渲染）— BSD 3-Clause License

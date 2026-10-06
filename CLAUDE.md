# FitAgent 项目规范
- Python / FastAPI / PostgreSQL / Milvus / pytest
- 运行时用 DeepSeek API (兼容 OpenAI SDK)
- 不要用 LangChain 包办，Agent Loop 必须手写
- 所有工具必须执行前校验权限，不能依赖模型判断
- 每次写代码前先说明改哪些文件，先写测试，再写实现
- README 只写项目本身（简介/技术栈/进度/评测）：不写「环境准备」「依赖启动」等搭建/运维内容（虚拟环境、依赖安装、服务拉起一律不写）
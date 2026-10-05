# FitAgent: 场馆智能运营 Agent

面向游泳馆/健身馆的智能体，支持会员、教练、管理员三角色，通过工具调用完成预约、取消、查询，并通过 Agentic RAG 回答场馆规则。

## 技术栈
- Python / PostgreSQL / Milvus
- DeepSeek API (Agent 大脑)
- Ollama (本地 Embedding)

## 当前进度 (第 1 周)
- [x] PostgreSQL 8 张业务表 + 模拟数据
- [x] Milvus 向量化知识库 (20 条文档)
- [x] 朴素 RAG 基线检索 (Top-1 命中验证)
- [ ] Agentic Query-Transformed RAG
- [ ] 业务工具调用与权限控制
- [ ] 评测体系

## 快速开始
1. 安装依赖: `pip install -r requirements.txt`
2. 配置环境: 复制 `.env.example` 为 `.env` 并填入密钥
3. 初始化数据: `python db/seed.py`
4. 跑测试: `pytest -v`
# AGENTS.md - Email HTTP Service

## 项目背景

服务器后端服务，主要功能：
1. **定时同步**：定期查看邮箱，更新邮件索引
2. **搜索查询**：接受 POST 请求，从索引中查找邮件，返回正文

---

## 文档索引

- **[FLOWS.md](./FLOWS.md)** — 完整功能流程文档（架构、同步、搜索、API、数据表）

---

## 开发流程

### 1. 本地开发测试

```bash
pip install -r requirements.txt
ruff check .
pytest -v
python app.py
```

### 2. 本地构建索引

```bash
rm -f data/email_index.db
python sync_test.py --stats
python sync_test.py --clear
```

> 详细同步流程见 [FLOWS.md](./FLOWS.md)

### 3. 上传服务器（需用户确认）

```bash
scp app.py email_indexer.py mail_client.py email_parser.py dingtalk_client.py ai_parser.py requirements.txt Dockerfile docker-compose.yml .dockerignore ubuntu@49.235.162.89:/home/ubuntu/email/

ssh ubuntu@49.235.162.89 "cd /home/ubuntu/email && sudo docker-compose build email-api && sudo docker-compose restart email-api"
```

### 4. 本地 HTTP 测试

```bash
curl http://49.235.162.89:8000/health

curl -X GET "http://49.235.162.89:8000/api/sync?mode=full" -H "X-API-Key: 13142467abcdefg"

curl -X POST "http://49.235.162.89:8000/api/extract-email" \
     -H "X-API-Key: 13142467abcdefg" \
     -H "Content-Type: application/json" \
     -d '{"title": "邮件标题"}'
```

---

## 项目结构

```
.
├── src/                # 代码文件夹（统一挂载到容器 /app/src）
│   ├── app.py              # FastAPI 主入口
│   ├── email_indexer.py    # 索引管理（增量/全量同步）
│   ├── email_parser.py     # 邮件解析（正文提取、签名去除）
│   ├── mail_client.py       # IMAP 客户端
│   ├── dingtalk_client.py   # 钉钉 API 客户端
│   ├── ai_parser.py         # AI 解析
│   ├── sync_test.py         # 独立同步脚本
│   └── requirements.txt      # Python 依赖
├── Dockerfile            # Docker 配置
├── docker-compose.yml    # Docker Compose 配置（代码卷挂载）
├── FLOWS.md             # 功能流程详细文档
├── .env                 # 环境变量
└── data/                # SQLite 索引数据库
```

---

## 环境变量 (.env)

```env
IMAP_SERVER=imap.263.net
IMAP_PORT=993
EMAIL_ACCOUNT=your@email.com
EMAIL_PASSWORD=your_password
API_KEY=13142467abcdefg
HOST=0.0.0.0
PORT=8000
AI_API_URL=https://api.deepseek.com/chat/completions
AI_API_KEY=your_ai_key
```

---

## 服务器管理

| 命令 | 说明 |
|------|------|
| `sudo docker ps -a \| grep email` | 查看容器状态 |
| `sudo docker logs email-ai-api -f` | 查看日志 |
| `sudo docker-compose restart email-api` | 重启服务 |
| `sudo docker-compose build email-api && up -d --build` | 重新构建 |

---

## 代码规范

### 导入顺序
```python
# 标准库
import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Dict, List, Optional

# 第三方库
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

# 本地导入
import mail_client
```

### 命名
- 类名: PascalCase
- 函数/方法: snake_case
- 常量: UPPER_SNAKE_CASE

# 邮件 HTTP 服务 - 功能流程总结

## 一、整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      IMAP 邮件服务器                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    mail_client.py                            │
│  - IMAP 连接管理（连接复用、断线重连）                         │
│  - 文件夹解析 (UTF-7)                                        │
│  - UID 提取                                                  │
│  - 邮件正文获取                                               │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   email_indexer.py                           │
│  - SQLite 本地索引                                          │
│  - 文件夹同步状态追踪 (folder_sync_meta)                      │
│  - 邮件搜索 (精确/模糊)                                       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      app.py (FastAPI)                       │
│  - HTTP API                                                 │
│  - 后台定时同步                                              │
└─────────────────────────────────────────────────────────────┘
```

## 二、数据表结构

### email_index — 邮件索引
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键自增 |
| msg_id | TEXT | UID（每文件夹唯一） |
| folder | TEXT | 文件夹名 |
| subject | TEXT | 邮件标题 |
| subject_lower | TEXT | 小写版本（搜索用） |
| from_addr | TEXT | 发件人 |
| date | DATETIME | 邮件日期 |
| indexed_at | DATETIME | 索引时间 |
| | | UNIQUE(folder, msg_id) |

### folder_sync_meta — 文件夹同步状态
| 字段 | 类型 | 说明 |
|------|------|------|
| folder | TEXT | 文件夹名（主键） |
| last_sync | DATETIME | 上次同步时间 |
| last_uids | TEXT | 上次同步的所有 UID（逗号分隔） |
| indexed_count | INTEGER | 已索引邮件数 |

## 三、同步流程

### 3.1 全量同步 (`sync_all_folders`)

```
1. mail.list("", "*") → 获取服务器所有文件夹
2. 遍历每个文件夹:
   ├─ mail.select(folder)
   ├─ get_folder_uids() → 获取 UID 列表用于计数
   ├─ _iter_by_uids(mail, len(uids)) → 批量获取邮件头部
   │   └─ mail.fetch('1:100', '(UID BODY[HEADER.FIELDS...])')
   │       └─ 正则提取 UID: r"UID (\d+)"
   │       └─ 每批 100 封邮件
   └─ add_or_update() → 写入 email_index
3. update_folder_sync_meta() → 更新每个文件夹的 UID 列表
```

### 3.2 增量同步 (`sync_incremental`)

```
1. 获取文件夹差异:
   ├─ indexed_folders = UNION(email_index.DISTINCT folder, folder_sync_meta.folder)
   ├─ new_folders = 服务器有，索引没有 → 全量同步
   ├─ deleted_folders = 索引有，服务器没有 → delete_folder
   └─ existing_folders → 增量同步
2. 新增文件夹: sync_all_folders 逻辑（全量同步）
3. 删除文件夹: delete_folder（删除 email_index + folder_sync_meta 记录）
4. 已有文件夹:
   ├─ current_uids = get_folder_uids()
   ├─ last_uids = folder_sync_meta.last_uids (逗号分隔转 set)
   ├─ new_uids = current - last → 新增邮件 → add_or_update
   ├─ gone_uids = last - current → 删除邮件 → delete
   └─ update_folder_sync_meta(..., last_uids=",".join(current_uids))
```

### 3.3 后台定时同步 (app.py)

```
启动时:
└─ threading.Thread(target=sync_index_task, daemon=True).start()

sync_index_task() 每 5 分钟循环:
├─ get_stats() 检查索引是否为空
├─ 索引为空 → 打印提示，等待手动全量同步
└─ 已有索引 → sync_incremental()
```

### 3.4 独立脚本 (sync_test.py)

| 命令 | 说明 |
|------|------|
| `python sync_test.py` | 全量同步 |
| `python sync_test.py --clear` | 清除旧索引后全量同步 |
| `python sync_test.py --stats` | 仅显示统计 |

## 四、搜索流程 (`/api/extract-email`)

```
1. search_by_subject(title)
   └─ 精确匹配: WHERE subject_lower = ? ORDER BY date DESC LIMIT 1

2. 未找到 → search_by_keywords(keywords)
   ├─ _extract_keywords() → 提取关键词（去除 Re: AW: 等前缀、特殊字符）
   └─ 模糊匹配: WHERE subject_lower LIKE '%kw%' ... ORDER BY date DESC LIMIT 50

3. 获取邮件正文
   └─ get_email_body_by_id(msg_id, folder)
       ├─ mail.select(folder)
       └─ mail.uid('FETCH', uid, '(RFC822)')

4. 去重签名
   └─ deduplicate_signature_blocks(body)

4.5 提取附件并转换
   └─ extract_all_attachments(msg_id, folder)
       ├─ 提取普通附件和内联图片
       ├─ 跳过 .p7s, .sig, .eml, .ai 签名/邮件格式文件
       └─ PPT/PPTX 文件自动转换为 PDF（LibreOffice headless）
           ├─ 保存临时文件
           ├─ 调用 libreoffice --headless --convert-to pdf
           ├─ 读取生成的 PDF
           └─ 清理临时文件（失败则回退到原始 PPT）

5. AI 解析（当前已注释）
   └─ ai_parser.parse_email_content()

6. 返回 ExtractResponse
```

## 五、API 端点

| 端点 | 方法 | 认证 | 说明 |
|------|------|------|------|
| `/health` | GET | 无 | 健康检查，返回索引统计 |
| `/api/sync?mode=full` | GET | X-API-Key | 手动全量同步 |
| `/api/sync?mode=incremental` | GET | X-API-Key | 手动增量同步 |
| `/api/extract-email` | POST | X-API-Key | 搜索邮件并提取正文 |

## 六、关键函数

### mail_client.py
| 函数 | 说明 |
|------|------|
| `get_connection()` | 获取/复用 IMAP 连接，断线自动重连 |
| `get_folder_uids(mail)` | `mail.uid('SEARCH', None, 'ALL')` 获取 UID 列表 |
| `get_email_body_by_id(msg_id, folder)` | 获取邮件正文（RFC822） |
| `decode_header_value()` | 解码邮件头部（UTF-7/Base64/Quoted-printable） |
| `deduplicate_signature_blocks()` | 去除重复的签名块 |
| `utf7_decode()` | UTF-7 编码解码 |

### email_indexer.py
| 函数 | 说明 |
|------|------|
| `_iter_by_uids(mail, msg_count, batch_size=500)` | 批量获取邮件头部 + UID（生成器） |
| `add_or_update(msg_id, folder, subject, from_addr, date)` | 插入/更新邮件索引 |
| `delete(folder, msg_id)` | 删除单封邮件索引 |
| `delete_folder(folder)` | 删除文件夹所有索引（email_index + folder_sync_meta） |
| `get_all_folders()` | 获取已追踪的文件夹（UNION email_index + folder_sync_meta） |
| `get_folder_sync_meta(folder)` | 获取文件夹同步状态 |
| `update_folder_sync_meta(folder, sync_time, last_uids, indexed_count)` | 更新同步状态 |
| `search_by_subject(title)` | 精确 + 模糊搜索 |
| `search_by_keywords(keywords)` | 关键词模糊搜索 |
| `sync_incremental(mail)` | 增量同步 |
| `sync_all_folders(mail)` | 全量同步 |

## 七、批处理配置

| 函数 | 默认批大小 |
|------|-----------|
| `_iter_by_uids()` | 100 封/批 |

## 八、UID 说明

- **UID 有效性**: `(folder, msg_id)` 组合才是全局唯一标识，因为某些服务器 UID 仅文件夹内唯一
- **UID 提取**: `_iter_by_uids` 通过 `mail.fetch('1:N', '(UID BODY[HEADER...])')` 提取，响应正则匹配 `r"UID (\d+)"`
- **跳过文件夹**: 通知文件夹 `&kBp35Q-*` 和过往订单 `&j8dfgIuiU1U-*` 和Delta业务/VMT 单证 通知`Delta&ThpSoQ-/VMT &U1WLwQ- &kBp35Q-`在同步时跳过

## 九、时区说明

- **存储格式**: naive datetime（无时区信息），统一为 +8 北京时间
- **转换函数**: `_parse_date_to_china_tz()` 将邮件原始 Date 头解析后转换为 +8 时区
  - UTC 时间 → +8 转换
  - 已经是 +8 的时间 → 保持不变
  - naive datetime → 假定为 UTC 后转换

## 十、开发命令

```bash
# 本地开发
cd src && pip install -r requirements.txt && cd ..
ruff check src/
pytest -v src/
python src/app.py

# 本地构建索引
rm -f data/email_index.db
python src/sync_test.py --stats
python src/sync_test.py --clear

# 上传服务器（代码卷挂载模式，更新代码只需 restart）
scp -r src/ Dockerfile docker-compose.yml .dockerignore \
   ubuntu@49.235.162.89:/home/ubuntu/email/

# 上传索引数据库（可选，如果需要同步最新索引）
scp -r data/ ubuntu@49.235.162.89:/home/ubuntu/email/

# 服务器启动容器（首次构建）
sudo docker-compose build email-api && sudo docker-compose up -d --build

# 代码更新后（无需重新构建，只需 restart）
sudo docker-compose restart email-api
sudo docker logs email-ai-api -f
```

## 十、服务器管理命令

```bash
sudo docker ps -a | grep email     # 查看容器状态
sudo docker logs email-ai-api -f   # 查看日志
sudo docker-compose restart email-api  # 重启服务（代码更新后使用）
```

## 十一、LibreOffice 配置（PPT 转 PDF）

### 当前模式：容器内安装

LibreOffice 在容器内通过 apt-get 安装，不依赖宿主机。

**Dockerfile 配置**：
```dockerfile
# 【关键】配置阿里云 Debian 源 (trixie对应Debian12)
RUN rm -rf /etc/apt/sources.list.d/* && \
    echo "deb http://mirrors.aliyun.com/debian bookworm main contrib non-free" > /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian bookworm-updates main contrib non-free" >> /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian-security bookworm-security main contrib non-free" >> /etc/apt/sources.list

# 安装 LibreOffice（包含所有依赖，包括 Impress 用于 PPT 转换）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libreoffice-impress \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
```

**docker-compose.yml**：无需挂载 LibreOffice 相关路径。

### 注意事项

- **不要**在 docker-compose.yml 中挂载宿主机的 LibreOffice（如 `/usr/lib/libreoffice`、`/usr/bin/soffice`）
- 容器内安装的优点：镜像自包含，构建后不依赖宿主机环境
- 缺点：镜像构建变慢（需下载 LibreOffice）

## 十二、调试邮件正文

### 本地调试（sync_test.py）

调试邮件正文提取，可以直接查看解析结果：

```bash
# 按标题搜索邮件
python sync_test.py --debug-email --subject "邮件标题"

# 按 UID 和文件夹直接定位
python sync_test.py --debug-email --uid 56 --folder "&i+JO9w-/Delta &i+JO9w-/2027 Q1/Wooden"
```

输出示例：
```
邮件信息:
  UID: 56
  文件夹: &i+JO9w-/Delta &i+JO9w-/2027 Q1/Wooden
  主题: AW: Re: AW: AM2604- Q1/2027 Wooden toys

正文预览 (前 500 字符):
---
Hi Stella
Noted, thank you.
...

正文总长度: 1437 字符

完整正文已保存到: data/debug_email_AW_Re_AW_AM2604_...
```

### 服务器调试

SSH 到服务器后：

```bash
# 连接服务器
ssh ubuntu@49.235.162.89

# 进入目录
cd /home/ubuntu/email

# 测试邮件正文提取（Python）
python3 -c "
import sys
sys.path.insert(0, 'src')
import mail_client
import email_parser

mail = mail_client.connect_mail()
# 文件夹名和 UID
folder = '&i+JO9w-/ASP &i+JO9w- '
body = email_parser._get_email_body_impl(mail, '697', folder)
print(body[:1000])
"
```

### 调试 tip

调试前先在本地确认代码正确：
```bash
cd src
python -c "
from email_parser import get_email_body
body = get_email_body('697', '&i+JO9w-/ASP &i+JO9w- ')
print(body[:1000])
"
```

确认本地正确后再上传服务器测试。

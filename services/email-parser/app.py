"""
邮件AI解析HTTP服务 - FastAPI主入口

功能：
- POST /api/extract-email: 根据标题搜索邮件，AI提取字段
- GET /health: 健康检查
- 本地索引 + 后台增量同步
"""

import os
import threading
import time
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

import dingtalk_client
import email_indexer
import email_parser
import mail_client

load_dotenv()

# API认证
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def sync_index_task():
    """后台增量同步任务（每10分钟执行一次）"""
    while True:
        try:
            mail = mail_client.get_sync_connection()
            indexer = email_indexer.get_indexer()

            stats = indexer.get_stats()
            if stats["total_emails"] == 0:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] 索引为空，跳过本次同步"
                )
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始增量同步邮件索引...")
                sync_stats = email_indexer.sync_incremental(mail, dedicated=True)
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] 同步完成: "
                    f"扫描{sync_stats['folders_scanned']}个文件夹, "
                    f"新增{sync_stats['emails_indexed']}封邮件"
                )
                if sync_stats["errors"]:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 同步错误: {sync_stats['errors'][:3]}")

        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 同步失败: {e}")

        time.sleep(600)


async def verify_api_key(api_key: Optional[str] = Depends(API_KEY_HEADER)):
    """验证API Key"""
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail={
                "status": "error",
                "error": "Missing API Key",
                "message": "请在 Header 中提供 X-API-Key",
            },
        )

    if api_key != os.getenv("API_KEY"):
        raise HTTPException(
            status_code=401,
            detail={
                "status": "error",
                "error": "Invalid API Key",
                "message": "API Key 无效",
            },
        )

    return api_key


# 请求/响应模型


class ExtractRequest(BaseModel):
    """提取邮件请求"""

    title: str = Field(..., min_length=1, description="邮件标题")
    record_id: str = Field(..., description="钉钉记录ID")
    user_id: str = Field(..., description="用户ID")


class ExtractResponse(BaseModel):
    """提取邮件响应"""

    status: str
    data: Optional[dict] = None
    error: Optional[str] = None
    message: Optional[str] = None


# 创建 FastAPI 应用
app = FastAPI(
    title="邮件AI解析服务",
    description="根据邮件标题搜索邮件，AI自动提取字段",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.on_event("startup")
async def startup_event():
    """服务启动时执行"""
    sync_thread = threading.Thread(target=sync_index_task, daemon=True)
    sync_thread.start()
    print("邮件服务已启动，后台增量同步任务运行中（每10分钟）")


@app.get("/health", tags=["健康检查"])
async def health_check():
    """
    健康检查端点

    用于 Docker healthcheck 和服务监控
    """
    indexer = email_indexer.get_indexer()
    stats = indexer.get_stats()

    return {
        "status": "healthy",
        "service": "email-ai-api",
        "version": "2.0.0",
        "index": stats,
    }


@app.post("/api/extract-email", response_model=ExtractResponse, tags=["邮件提取"])
async def extract_email(request: ExtractRequest, _api_key: str = Depends(verify_api_key)):
    """
    根据标题提取邮件并AI解析

    - 根据提供的 title 在本地索引中搜索
    - 获取邮件全文
    - 调用 AI 提取字段
    - 返回结构化数据
    """
    try:
        indexer = email_indexer.get_indexer()

        # 1. 先从本地索引搜索
        email_info = indexer.search_by_subject(request.title)

        if not email_info:
            # 如果本地没找到，尝试用关键词搜索
            keywords = indexer._extract_keywords_for_match(request.title)
            if keywords:
                matches = indexer.search_by_keywords(keywords)
                # 找精确匹配的
                title_lower = request.title.strip().lower()
                for m in matches:
                    if m["subject_lower"].strip() == title_lower:
                        email_info = m
                        break

        if not email_info:
            raise HTTPException(
                status_code=404,
                detail={
                    "status": "error",
                    "error": "EmailNotFound",
                    "message": f"未找到标题为 '{request.title}' 的邮件",
                },
            )

        # 2. 获取邮件正文（使用索引中的ID）
        body = email_parser.get_email_body(email_info["msg_id"], email_info["folder"])

        if not body:
            raise HTTPException(
                status_code=500,
                detail={
                    "status": "error",
                    "error": "BodyNotFound",
                    "message": "邮件正文为空或获取失败",
                },
            )

        # 提取附件
        attachments_result = []
        attachments_for_upload: list = []
        try:
            attachments = email_parser.extract_all_attachments(
                email_info["msg_id"], email_info["folder"]
            )
            if attachments:
                # 格式转换：[(filename, mime_type, file_data), ...] -> [(file_data, filename, mime_type), ...]
                attachments_for_upload = [
                    (file_data, filename, mime_type)
                    for filename, mime_type, file_data in attachments
                ]
        except Exception as e:
            print(f"提取附件失败: {e}")

        # 始终更新正文和附件
        try:
            upload_results = dingtalk_client.update_record_with_email_and_attachments(
                record_id=request.record_id,
                uid=request.user_id,
                email_body=body,
                attachments=attachments_for_upload,
            )
            for result in upload_results:
                attachments_result.append(
                    {
                        "filename": result.get("filename", ""),
                        "status": "success",
                        "resource_id": result.get("resourceId"),
                        "resource_url": result.get("url"),
                    }
                )
        except Exception as e:
            print(f"更新记录失败: {e}")
            for filename, mime_type, file_data in attachments_for_upload:
                attachments_result.append(
                    {
                        "filename": filename,
                        "status": "error",
                        "error": str(e),
                    }
                )

        # 3. AI 解析
        # parsed_data = ai_parser.parse_email_content(
        #     subject=email_info["subject"],
        #     from_addr=email_info.get("from_addr", ""),
        #     body=body,
        # )

        # 4. 返回结果
        return ExtractResponse(
            status="success",
            data={
                "email": {
                    "subject": email_info["subject"],
                    "from": email_info.get("from_addr", ""),
                    "date": email_info.get("date"),
                    "body_preview": body[:10000] + "..." if len(body) > 10000 else body,
                    "folder": email_info.get("folder_display", email_info.get("folder", "")),
                },
                "attachments": attachments_result,
                # "extracted": parsed_data,
            },
            message="邮件提取成功",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error": "InternalError",
                "message": f"服务器内部错误: {str(e)}",
            },
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("DEBUG", "false").lower() == "true",
    )

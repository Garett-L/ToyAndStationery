"""
钉钉多维表API客户端模块

提供多维表附件上传和记录更新功能
"""

import os
import time
from typing import List, Tuple

import requests
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 常量配置
APPKEY = os.getenv("APPKEY")
APPSECRET = os.getenv("APPSECRET")
BASE_ID = "1zknDm0WRaA0Obv1t2N1bwYZ8BQEx5rG"
SHEET_ID = "44S5wrE"
ATTACHMENT_FIELD_ID = "3dONfyg"  # 附件字段
BODY_FIELD_ID = "t3AU1vZ"  # 原始邮件内容字段

DINGTALK_API_BASE = "https://api.dingtalk.com"

# Token缓存
_access_token_cache = {"token": None, "expires_at": 0}


def get_access_token() -> str:
    """
    获取钉钉访问令牌

    使用 APPKEY/APPSECRET 获取 accessToken，支持自动缓存

    Returns:
        str: 有效的 accessToken
    """
    global _access_token_cache

    # 检查缓存是否有效（提前5分钟过期避免临界情况）
    current_time = time.time()
    if _access_token_cache["token"] and _access_token_cache["expires_at"] > current_time + 300:
        return _access_token_cache["token"]

    # 请求新token
    url = f"{DINGTALK_API_BASE}/v1.0/oauth2/accessToken"
    payload = {"appKey": APPKEY, "appSecret": APPSECRET}

    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()

    result = response.json()

    if result.get("accessToken"):
        _access_token_cache["token"] = result["accessToken"]
        # 钉钉token有效期为2小时，缓存1.8小时
        _access_token_cache["expires_at"] = current_time + 3600 * 1.8
        return result["accessToken"]
    else:
        raise Exception(f"获取accessToken失败: {result}")


def get_union_id_by_uid(token: str, uid: str) -> str:
    """
    通过用户uid获取unionId

    使用旧版 API /topapi/v2/user.get 获取 unionId

    Args:
        token: 访问令牌
        uid: 用户的uid

    Returns:
        str: unionId
    """
    url = "https://oapi.dingtalk.com/topapi/v2/user/get"
    params = {"access_token": token}
    body = {"userid": uid}

    response = requests.post(url, json=body, params=params, timeout=30)
    response.raise_for_status()

    result = response.json()

    if result.get("errcode") == 0:
        union_id = result.get("result", {}).get("unionid")
        if union_id:
            return union_id
        else:
            raise Exception(f"响应中未找到unionId: {result}")
    else:
        raise Exception(f"获取unionId失败: {result}")


def get_upload_info(
    token: str, base_id: str, operator_id: str, file_size: int, mime_type: str, filename: str
) -> dict:
    """
    获取上传URL和resourceId

    Args:
        token: 访问令牌
        base_id: 多维表ID
        operator_id: 操作员ID（unionId）
        file_size: 文件大小（字节）
        mime_type: MIME类型 (如 "image/png", "application/pdf")
        filename: 文件名

    Returns:
        dict: 包含 uploadUrl, resourceId, resourceUrl 的字典
    """
    url = f"{DINGTALK_API_BASE}/v1.0/doc/docs/resources/{base_id}/uploadInfos/query"
    headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}
    params = {"operatorId": operator_id}
    payload = {"size": file_size, "mediaType": mime_type, "resourceName": filename}

    response = requests.post(url, json=payload, params=params, headers=headers, timeout=30)
    response.raise_for_status()

    result = response.json()

    # 判断是否成功：result 字段存在表示成功，errcode 存在且非0表示失败
    if result.get("result") is not None:
        return result["result"]
    elif result.get("errcode") and result.get("errcode") != 0:
        raise Exception(f"获取上传信息失败: {result}")
    else:
        raise Exception(f"获取上传信息失败: {result}")


def upload_file_to_url(upload_url: str, file_data: bytes, mime_type: str) -> bool:
    """
    上传文件到指定URL

    Args:
        upload_url: 上传URL
        file_data: 文件二进制数据
        mime_type: MIME类型

    Returns:
        bool: 上传是否成功
    """
    upload_headers = {"Content-Type": mime_type}

    response = requests.put(upload_url, data=file_data, headers=upload_headers, timeout=60)

    if response.status_code != 200:
        raise Exception(f"上传文件失败: status={response.status_code}, response={response.text}")

    return True


def upload_attachments(
    token: str, base_id: str, operator_id: str, attachments: List[Tuple[bytes, str, str]]
) -> List[dict]:
    """
    多附件批量处理

    遍历每个附件获取上传URL并上传，返回附件信息列表

    Args:
        token: 访问令牌
        base_id: 多维表ID
        operator_id: 操作员ID（unionId）
        attachments: 附件列表，格式为 [(file_data: bytes, filename: str, mime_type: str), ...]

    Returns:
        List[dict]: 附件信息列表，格式为 [{"filename": str, "size": int, "type": str, "url": str, "resourceId": str}, ...]
    """
    results = []

    for file_data, filename, mime_type in attachments:
        # 获取上传信息
        upload_info = get_upload_info(
            token, base_id, operator_id, len(file_data), mime_type, filename
        )

        # 上传文件
        upload_file_to_url(upload_info["uploadUrl"], file_data, mime_type)

        # 收集附件信息
        results.append(
            {
                "filename": filename,
                "size": len(file_data),
                "type": mime_type,
                "url": upload_info["resourceUrl"],
                "resourceId": upload_info["resourceId"],
            }
        )

    return results


def update_record_attachments(
    token: str, base_id: str, sheet_id: str, record_id: str, field_id: str, attachments: List[dict]
) -> bool:
    """
    更新多维表记录的附件字段

    Args:
        token: 访问令牌
        base_id: 多维表ID
        sheet_id: 工作表ID
        record_id: 记录ID
        field_id: 附件字段ID
        attachments: 附件列表，格式为 [{"filename": "...", "size": N, "type": "...", "url": "...", "resourceId": "..."}]

    Returns:
        bool: 更新是否成功
    """
    url = f"{DINGTALK_API_BASE}/v1.0/notable/bases/{base_id}/sheets/{sheet_id}/records"
    headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}
    params = {
        "operatorId": token.split(".")[0] if "." in token else ""
    }  # 临时占位，实际调用时需传入operator_id

    # 注意：实际使用时 operatorId 应该通过调用方传入，这里仅作占位
    payload = {"records": [{"id": record_id, "fields": {field_id: attachments}}]}

    response = requests.put(url, json=payload, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    result = response.json()

    # 判断是否成功：value 字段存在表示成功，errcode 存在且非0表示失败
    if result.get("value") is not None:
        return True
    elif result.get("errcode") and result.get("errcode") != 0:
        raise Exception(f"更新记录失败: {result}")
    else:
        return True


def update_record_with_email_and_attachments(
    record_id: str, uid: str, email_body: str, attachments: List[Tuple[bytes, str, str]]
) -> List[dict]:
    """
    完整流程便捷函数：上传附件并更新邮件正文到多维表记录

    Args:
        record_id: 记录ID
        uid: 用户的uid（如果是 staffId 则直接作为 operatorId）
        email_body: 邮件正文内容
        attachments: 附件列表，格式为 [(file_data: bytes, filename: str, mime_type: str), ...]

    Returns:
        List[dict]: 上传后的附件信息列表
    """
    token = get_access_token()

    # 尝试获取unionId作为operatorId
    try:
        operator_id = get_union_id_by_uid(token, uid)
    except Exception as e:
        # 如果获取失败（60121 找不到用户），使用传入的uid直接作为operatorId
        print(f"获取unionId失败，使用uid作为operatorId: {e}")
        operator_id = uid

    # 一次性更新记录（包含邮件正文和附件）
    url = f"{DINGTALK_API_BASE}/v1.0/notable/bases/{BASE_ID}/sheets/{SHEET_ID}/records"
    headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}
    params = {"operatorId": operator_id}

    # 构建字段更新数据
    fields = {"原始邮件内容": email_body}
    attachment_results: List[dict] = []

    # 批量上传附件（如果有）
    if attachments:
        attachment_results = upload_attachments(token, BASE_ID, operator_id, attachments)
        fields["图片和附件"] = attachment_results

    # 一次性更新记录
    payload = {"records": [{"id": record_id, "fields": fields}]}
    response = requests.put(url, json=payload, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    result = response.json()

    # 判断是否成功：value 字段存在表示成功，errcode 存在且非0表示失败
    if result.get("value") is not None:
        pass  # 有 value 表示成功
    elif result.get("errcode") and result.get("errcode") != 0:
        raise Exception(f"更新记录失败: {result}")

    return attachment_results if attachments else []


def format_attachment_info(
    filename: str, size: int, mime_type: str, resource_id: str, resource_url: str
) -> dict:
    """
    工具函数：格式化附件信息

    Args:
        filename: 文件名
        size: 文件大小（字节）
        mime_type: MIME类型
        resource_id: 资源ID
        resource_url: 资源URL

    Returns:
        dict: 格式化的附件信息
    """
    return {
        "filename": filename,
        "size": size,
        "type": mime_type,
        "url": resource_url,
        "resourceId": resource_id,
    }

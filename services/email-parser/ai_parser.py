"""
AI 邮件解析模块

从邮件内容中提取结构化字段
"""

import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()


def parse_email_content(subject: str, from_addr: str, body: str) -> dict:
    """
    使用 AI 解析邮件内容，提取字段

    Args:
        subject: 邮件标题
        from_addr: 发件人地址
        body: 邮件正文

    Returns:
        dict: 提取的字段数据
    """
    # 字段定义（可配置，用户稍后提供）
    # 这里使用通用字段，后续可以根据需求调整
    fields = [
        "客户名称",
        "联系人",
        "联系电话",
        "邮箱",
        "产品名称",
        "产品数量",
        "需求描述",
        "收到日期",
    ]

    # 构建 prompt
    prompt = f"""请从以下邮件内容中提取信息，转换为JSON格式。

邮件标题: {subject}
发件人: {from_addr}
邮件内容:
{body[:5000]}

请从邮件中提取以下字段：
{", ".join(fields)}

请返回JSON格式（只返回JSON，不要其他内容）：
{{
    "客户名称": "从邮件中提取的客户名称，如没有则填null",
    "联系人": "从邮件中提取的联系人姓名，如没有则填null",
    "联系电话": "从邮件中提取的电话号码，如没有则填null",
    "邮箱": "从邮件中提取的邮箱地址，如没有则填null",
    "产品名称": "从邮件中提取的产品名称，如没有则填null",
    "产品数量": "从邮件中提取的产品数量，如没有则填null",
    "需求描述": "从邮件中提取的需求描述，如没有则填null",
    "收到日期": "从邮件中提取的日期信息，如没有则填null"
}}

注意：
- 只返回JSON，不要包含markdown标记
- 如果某个字段在邮件中找不到，请填null
- 电话号码可以是手机或座机
- 产品数量请包含单位，如"100台"、"500件"等
"""

    try:
        response = requests.post(
            os.getenv("AI_API_URL", "https://api.deepseek.com/chat/completions"),
            headers={
                "Authorization": f"Bearer {os.getenv('AI_API_KEY', '')}",
                "Content-Type": "application/json",
            },
            json={
                "model": os.getenv("AI_MODEL", "deepseek-chat"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=int(os.getenv("AI_TIMEOUT", "60")),
        )

        if response.status_code == 200:
            result = response.json()
            content = result["choices"][0]["message"]["content"]

            # 清理AI返回的JSON
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

            # 解析JSON
            try:
                data = json.loads(content)
                return data
            except json.JSONDecodeError:
                # 如果解析失败，返回原始内容
                return {"raw_response": content, "error": "JSON解析失败"}
        else:
            return {"error": f"AI调用失败: {response.status_code}"}

    except requests.exceptions.Timeout:
        return {"error": "AI调用超时"}
    except Exception as e:
        return {"error": f"AI调用异常: {str(e)}"}


def parse_email_with_custom_fields(subject: str, from_addr: str, body: str, fields: list) -> dict:
    """
    使用自定义字段解析邮件内容

    Args:
        subject: 邮件标题
        from_addr: 发件人地址
        body: 邮件正文
        fields: 自定义字段列表

    Returns:
        dict: 提取的字段数据
    """
    # 构建字段描述
    field_desc = "\n".join([f"- {f}" for f in fields])

    prompt = f"""请从以下邮件内容中提取信息，转换为JSON格式。

邮件标题: {subject}
发件人: {from_addr}
邮件内容:
{body[:5000]}

请从邮件中提取以下字段：
{field_desc}

请返回JSON格式（只返回JSON，不要其他内容）。
每个字段的值从邮件中提取，如果找不到则填null。

注意：
- 只返回JSON，不要包含markdown标记
- 如果某个字段在邮件中找不到，请填null
"""

    try:
        response = requests.post(
            os.getenv("AI_API_URL", "https://api.deepseek.com/chat/completions"),
            headers={
                "Authorization": f"Bearer {os.getenv('AI_API_KEY', '')}",
                "Content-Type": "application/json",
            },
            json={
                "model": os.getenv("AI_MODEL", "deepseek-chat"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=int(os.getenv("AI_TIMEOUT", "60")),
        )

        if response.status_code == 200:
            result = response.json()
            content = result["choices"][0]["message"]["content"]

            # 清理JSON
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

            return json.loads(content)
        else:
            return {"error": f"AI调用失败: {response.status_code}"}

    except Exception as e:
        return {"error": str(e)}

"""
邮件正文与附件统一提取模块

提供邮件正文获取和附件提取功能，支持:
- 邮件正文提取（text/plain 优先，text/html 降级）
- 普通附件提取（Content-Disposition: attachment）
- 内联图片提取（Content-Disposition: inline 或 Content-ID）
- 签名块去重
"""

import os
import shutil
import subprocess
import tempfile
from email.header import decode_header
from email.parser import BytesParser
from typing import List, Tuple

import mail_client

# ============ 邮件链解析常量 ============
# 可在此处统一添加新的邮件头标志和正文标志

# 邮件头开始标志（遇到这些标志开始新的历史邮件块）
# 注意：冒号后允许零个或多个空白
EMAIL_HEADER_START_RE = r"^(Von:|From:|发件人：)\s*"

# 邮件头字段标志（新字段开始，换行）
EMAIL_HEADER_FIELD_RE = (
    r"^(Gesendet:|Date:|Sent:|发送时间：|An:|To:|收件人：|Cc:|抄送：|Betreff:|Subject:|主题：)\s*"
)

# 正文开始标志（遇到这些标志进入正文模式）
EMAIL_BODY_START_RE = r"^(Hi|Hello|Hola|Dear|Hi\s|Hello\s|Hola\s|Dear\s)"

# 签名标志（进入正文后，遇到这些标志开始签名）
EMAIL_SIG_RE = r"^(W/Regards\.?|Mit freundlichen Grüßen|此致敬意)"

# ============ 邮件正文提取 ============


def get_email_body(msg_id: str, folder: str = "INBOX") -> str:
    """
    获取邮件正文，使用独立连接（避免与后台同步冲突）

    Args:
        msg_id: 邮件 UID
        folder: 文件夹名称

    Returns:
        邮件正文字符串
    """
    mail = None
    try:
        # 创建独立连接，不与后台同步共享
        mail = mail_client.connect_mail()
        return _get_email_body_impl(mail, msg_id, folder)
    except Exception as e:
        print(f"获取邮件正文失败: {e}")
        return ""
    finally:
        # 关闭独立连接
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


def _get_email_body_impl(mail, msg_id, folder_name: str = "INBOX") -> str:
    """
    获取邮件正文实现（内部函数）

    Args:
        mail: 已连接的 IMAP 连接
        msg_id: 邮件 ID
        folder_name: 文件夹名

    Returns:
        邮件正文内容
    """
    try:
        mail.select(mail_client.quote_folder_name(folder_name))
        status, data = mail.uid("FETCH", msg_id, "(RFC822)")

        if not data or not data[0]:
            return ""

        msg = BytesParser().parsebytes(data[0][1])
        body = ""
        html_body = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                # 优先获取 text/html（保留表格结构）
                if content_type == "text/html" and not html_body:
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            encoding = part.get_content_charset()
                            html_body = _decode_payload(payload, encoding)
                    except Exception:
                        pass
                # 降级获取 text/plain
                elif content_type == "text/plain" and not body:
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            encoding = part.get_content_charset()
                            body = _decode_payload(payload, encoding)
                    except Exception:
                        pass
        else:
            content_type = msg.get_content_type()
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    encoding = msg.get_content_charset()
                    decoded = _decode_payload(payload, encoding)
                    if content_type == "text/html":
                        html_body = decoded
                    else:
                        body = decoded
            except Exception:
                body = str(msg.get_payload())

        # 优先使用 text/html（保留表格结构），没有则降级到 text/plain
        if html_body:
            body = _strip_html(html_body)
        elif not body:
            body = str(msg.get_payload()) if msg.get_payload() else ""

        if len(body) > int(mail_client.os.getenv("EMAIL_BODY_MAX_SIZE", str(1024 * 1024))):
            body = (
                body[: int(mail_client.os.getenv("EMAIL_BODY_MAX_SIZE", str(1024 * 1024)))]
                + "\n\n[内容已截断]"
            )

        body = _strip_signature(body)

        # 添加邮件头前缀（先解码 RFC 2047 编码，再去除换行符）
        def clean_header(value):
            """解码邮件头并去除换行符"""
            if not value:
                return None
            # 先解码 RFC 2047 编码（如 =?utf-8?B?...?= 或 =?iso-8859-1?Q?...?=）
            value = mail_client.decode_header_value(value)
            # 把换行符替换为空格
            value = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            # 合并多余空格
            import re

            value = re.sub(r"\s+", " ", value)
            return value.strip()

        def parse_date_to_china_tz(date_str):
            """解析邮件日期并转换为东八区（+8）"""
            from datetime import datetime, timezone, timedelta

            if not date_str:
                return None
            CHINA_TZ = timezone(timedelta(hours=8))
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(date_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(CHINA_TZ).replace(tzinfo=None)
            except Exception:
                return None

        header_parts = []
        from_val = clean_header(msg.get("From"))
        to_val = clean_header(msg.get("To"))
        subject_val = clean_header(msg.get("Subject"))

        # 日期字段转换为东八区
        raw_date = msg.get("Date")
        date_val = None
        if raw_date:
            china_date = parse_date_to_china_tz(raw_date)
            if china_date:
                date_val = china_date.strftime("%Y-%m-%d %H:%M:%S")

        if from_val:
            header_parts.append(f"From: {from_val}")
        if to_val:
            header_parts.append(f"To: {to_val}")
        if subject_val:
            header_parts.append(f"Subject: {subject_val}")
        if date_val:
            header_parts.append(f"Date: {date_val}")

        if header_parts:
            body = "\n".join(header_parts) + "\n\n" + body

        return body

    except Exception as e:
        print(f"获取正文失败: {e}")
        return ""


def _decode_payload(payload, encoding):
    """解码邮件 payload"""
    if encoding and encoding.lower() in ["unknown-8bit", "gb2312", "gbk", "gb18030"]:
        encoding = "utf-8"
    return payload.decode(encoding or "utf-8", errors="replace")


# ============ Von: 块解析 (德语邮件转发块) ============


def _strip_html(text):
    """
    去除 HTML 标签，保留表格结构，按纯文本逻辑处理邮件链。

    邮件链用 "\n\n---\n" 分割。
    """
    import re

    if not text:
        return ""

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")

        # 去除垃圾标签
        for tag in soup.find_all(["o:p", "v:shapetype", "v:shape", "w:worddocument", "m:oMath"]):
            tag.decompose()

        ws = soup.find("div", class_="WordSection1")
        if not ws:
            return _strip_html_fallback(text)

        def extract_lines_from_element(elem):
            """从元素提取纯文本行"""
            name = getattr(elem, "name", None)
            if name == "table":
                return _convert_table_to_text_simple(elem)
            elif name == "blockquote":
                # blockquote 的直接子元素分别处理
                lines = []
                for subchild in elem.children:
                    lines.extend(extract_lines_from_element(subchild))
                return lines
            elif hasattr(elem, "get_text"):
                txt = elem.get_text()
                # 统一换行符（处理 \r\n 和 \r），再分割
                txt = txt.replace("\r\n", "\n").replace("\r", "\n")
                return [line.strip() for line in txt.split("\n") if line.strip()]
            return []

        # 收集所有纯文本内容（按行拆分）
        all_lines = []

        for child in ws.children:
            name = getattr(child, "name", None)
            if name == "blockquote":
                # blockquote 递归处理
                all_lines.extend(extract_lines_from_element(child))
                continue

            all_lines.extend(extract_lines_from_element(child))

        # ========== 纯文本邮件链处理 ==========
        output_parts = []
        current_email_lines = []
        in_signature = False
        body_started = False  # 是否已进入正文

        # 编译正则（使用文件顶部定义的常量）
        HEADER_RE = re.compile(EMAIL_HEADER_START_RE, re.I)
        HEADER_FIELD_RE = re.compile(EMAIL_HEADER_FIELD_RE, re.I)
        BODY_START_RE = re.compile(EMAIL_BODY_START_RE, re.I)
        SIG_RE = re.compile(EMAIL_SIG_RE, re.I)

        for line in all_lines:
            stripped = line.strip()
            if not stripped:
                continue

            # 碰到正文开始标志 → 进入正文模式
            if BODY_START_RE.match(stripped):
                if current_email_lines:
                    current_email_lines.append("")  # 空行分隔邮件头和正文
                    current_email_lines.append(stripped)
                body_started = True
                continue

            # 碰到邮件头开始标志 → 开始新的邮件块
            if HEADER_RE.match(stripped):
                if current_email_lines:
                    output_parts.append("\n".join(current_email_lines))
                current_email_lines = [stripped]
                in_signature = False
                body_started = False
                continue

            # 进入正文后，处理签名
            if body_started:
                if SIG_RE.match(stripped):
                    in_signature = True
                if in_signature:
                    continue
                current_email_lines.append(stripped)
            else:
                # 邮件头区域内
                if HEADER_FIELD_RE.match(stripped):
                    # 新字段开始，换行
                    current_email_lines.append(stripped)
                else:
                    # 同一字段的内容，合并到上一行
                    if current_email_lines:
                        current_email_lines[-1] = current_email_lines[-1] + " " + stripped
                    else:
                        current_email_lines.append(stripped)

        if current_email_lines:
            output_parts.append("\n".join(current_email_lines))

        # 合并，邮件链用 "\n\n---\n" 分割
        result = "\n\n---\n".join(output_parts)

        # 清理
        result = result.replace("&nbsp;", " ")
        result = result.replace("&lt;", "<")
        result = result.replace("&gt;", ">")

        # 合并行内多余空格（不去除空行，保留邮件链分隔）
        result = re.sub(r" {2,}", " ", result)

        return result.strip()

    except Exception:
        import traceback

        traceback.print_exc()
        return _strip_html_fallback(text)


def _convert_table_to_text_simple(table):
    """简单表格转文本（无表头分隔符）"""
    rows = []
    for tr in table.find_all("tr"):
        cells = []
        for td in tr.find_all(["td", "th"]):
            txt = td.get_text(strip=True)
            cells.append(txt if txt else "")
        if cells:
            rows.append("| " + " | ".join(cells) + " |")
    return rows


def _strip_html_fallback(text):
    """降级使用的简单 HTML 去除（不保留表格结构）"""
    text = mail_client.re.sub(
        r"<script[^>]*>.*?</script>",
        "",
        text,
        flags=mail_client.re.DOTALL | mail_client.re.IGNORECASE,
    )
    text = mail_client.re.sub(
        r"<style[^>]*>.*?</style>",
        "",
        text,
        flags=mail_client.re.DOTALL | mail_client.re.IGNORECASE,
    )
    text = mail_client.re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&amp;", "&")
    text = text.replace("&quot;", '"')
    text = mail_client.re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_signature(text):
    """
    去除邮件签名

    邮件链已在 HTML 解析阶段处理（删除了历史回复），
    这里只处理单封邮件的签名去除。
    """
    # 标准签名分隔符：-- 单独一行（RFC 5322）
    if mail_client.re.search(r"^--\s*$", text, mail_client.re.MULTILINE):
        text = mail_client.re.split(r"^--\s*$", text, flags=mail_client.re.MULTILINE)[0]

    # 常见签名关键词（在行首，单独一行）
    text = mail_client.re.sub(r"(?i)\n{1,2}发自.*$", "", text)
    text = mail_client.re.sub(r"(?i)\n{1,2}sent from my.*$", "", text)
    text = mail_client.re.sub(
        r"(?i)\n{1,2}(best regards|thanks|regards|cheers|sincerely|yours truly|kind regards)\s*[,:\n].*$",
        "",
        text,
    )
    text = mail_client.re.sub(r"(?i)\n{1,2}(原号码|手机|微信).*$", "", text)

    return text.strip()


def _extract_body_from_msg(msg) -> Tuple[str, str]:
    """
    从邮件消息中提取正文

    Returns:
        Tuple of (body_text, html_body)
    """
    body_text = ""
    html_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain" and not body_text:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        encoding = part.get_content_charset()
                        body_text = _decode_payload(payload, encoding) or ""
                except Exception:
                    pass
            elif content_type == "text/html" and not html_body:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        encoding = part.get_content_charset()
                        html_body = _decode_payload(payload, encoding) or ""
                except Exception:
                    pass
    else:
        content_type = msg.get_content_type()
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                encoding = msg.get_content_charset()
                decoded = _decode_payload(payload, encoding) or ""
                if content_type == "text/html":
                    html_body = decoded
                else:
                    body_text = decoded
        except Exception:
            pass

    return body_text, html_body


def _find_signature_position_in_html(html_body: str) -> int:
    """
    找到HTML正文中签名的起始位置

    策略：找到真正的签名关键词（不是 "thanks" 这种模糊的），
    然后保留这个签名之前的所有图片

    Args:
        html_body: HTML正文

    Returns:
        签名在HTML中的字符位置，如果没有找到则返回 -1
    """
    if not html_body:
        return -1

    # 跳过 HTML <head> 部分
    body_start = html_body.lower().find("<body")
    search_start = body_start if body_start >= 0 else 0

    # 明确的签名关键词（不包含模糊的 "thanks"）
    # 找第一个这样明确签名的位置
    html_lower = html_body.lower()
    clear_sig_keywords = [
        "mit freundlichen grüßen",
        "best regards",
        "kind regards",
        "yours truly",
        "sincerely yours",
        "regards",
    ]

    best_pos = -1
    for keyword in clear_sig_keywords:
        pos = html_lower.find(keyword, search_start)
        if pos >= 0 and (best_pos < 0 or pos < best_pos):
            best_pos = pos

    return best_pos


def _extract_cid_references_from_html(html_body: str, signature_pos: int) -> list:
    """
    从HTML正文中按顺序提取cid引用

    Args:
        html_body: HTML正文
        signature_pos: 签名在HTML中的位置

    Returns:
        按出现顺序排列的cid列表，只包含签名之前的引用
    """
    if not html_body:
        return []

    # 只在签名之前的HTML中查找
    if signature_pos > 0:
        search_text = html_body[:signature_pos]
    else:
        # 没有找到签名，返回空（不提取任何图片）
        return []

    # 查找所有 <img ... src="cid:xxx" ...> 模式
    cid_pattern = mail_client.re.compile(
        r'<img[^>]+src=["\']cid:([^"\'>\s]+)["\'][^>]*>', mail_client.re.IGNORECASE
    )

    cids = []
    for match in cid_pattern.finditer(search_text):
        cids.append(match.group(1).lower())

    return cids


# ============ 附件提取 ============


def extract_attachments(msg_id: str, folder: str) -> List[Tuple[str, str, bytes]]:
    """
    提取普通附件（Content-Disposition: attachment）

    Args:
        msg_id: 邮件 UID
        folder: 文件夹名称

    Returns:
        附件列表，每个元素为 (filename, content_type, data) 元组
    """
    mail = None
    try:
        mail = mail_client.connect_mail()
        mail.select(mail_client.quote_folder_name(folder))
        status, data = mail.uid("FETCH", msg_id, "(RFC822)")
        if status != "OK" or not data:
            return []

        msg = BytesParser().parsebytes(data[0][1])
        attachments = []

        for part in msg.walk():
            content_disposition = part.get_content_disposition() or ""
            if "attachment" not in content_disposition.lower():
                continue

            filename = part.get_filename()
            if not filename:
                continue
            filename = decode_filename(filename)

            # 跳过签名文件、邮件格式文件和 .ai 文件
            if filename.lower().endswith((".p7s", ".sig", ".eml", ".ai")):
                continue
            if filename:
                filename = decode_filename(filename)
            else:
                continue

            content_type = part.get_content_type() or "application/octet-stream"
            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            attachments.append((filename, content_type, payload))

        return attachments

    except Exception as e:
        print(f"提取附件失败: {e}")
        return []
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


def extract_inline_images(msg_id: str, folder: str) -> List[Tuple[str, str, bytes]]:
    """
    提取内联图片（Content-Disposition: inline 或 Content-ID）

    只保留签名前的图片，过滤掉签名中的图片

    Args:
        msg_id: 邮件 UID
        folder: 文件夹名称

    Returns:
        内联图片列表，每个元素为 (filename, content_type, data) 元组
    """
    mail = None
    try:
        mail = mail_client.connect_mail()
        mail.select(mail_client.quote_folder_name(folder))
        status, data = mail.uid("FETCH", msg_id, "(RFC822)")
        if status != "OK" or not data:
            return []

        msg = BytesParser().parsebytes(data[0][1])

        # 获取正文和HTML
        body_text, html_body = _extract_body_from_msg(msg)
        if not body_text and html_body:
            body_text = _strip_html(html_body)

        # 找到签名在HTML中的位置
        html_signature_pos = _find_signature_position_in_html(html_body)

        # 从HTML中按顺序提取所有被引用的cid
        cid_order = _extract_cid_references_from_html(html_body, html_signature_pos)

        # 提取所有内联图片
        counter = 1
        cid_to_image = {}  # cid -> (filename, content_type, data)

        for part in msg.walk():
            content_disposition = part.get_content_disposition()
            content_id = part.get("Content-ID")
            content_type = part.get_content_type() or ""

            is_inline = (
                content_disposition is not None and "inline" in content_disposition.lower()
            ) or content_id is not None

            if not is_inline:
                continue

            if not content_type.startswith("image/"):
                continue

            filename = part.get_filename()
            if filename:
                filename = decode_filename(filename)
            else:
                ext = get_extension_from_mime(content_type)
                filename = f"inline_image_{counter}{ext}"
                counter += 1

            # 跳过签名文件、邮件格式文件和 .ai 文件
            if filename.lower().endswith((".p7s", ".sig", ".eml", ".ai")):
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            # 用cid作为key来建立映射
            if content_id:
                cid_key = content_id.strip("<>").lower()
                cid_to_image[cid_key] = (filename, content_type, payload)

        # 按HTML中的引用顺序返回图片
        result = []
        for cid in cid_order:
            if cid in cid_to_image:
                result.append(cid_to_image[cid])

        return result

    except Exception as e:
        print(f"提取内联图片失败: {e}")
        return []
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


def extract_all_attachments(msg_id: str, folder: str) -> List[Tuple[str, str, bytes]]:
    """
    提取所有附件（普通附件 + 内联图片）

    Args:
        msg_id: 邮件 UID
        folder: 文件夹名称

    Returns:
        所有附件列表，每个元素为 (filename, content_type, data) 元组
    """
    attachments = extract_attachments(msg_id, folder)
    inline_images = extract_inline_images(msg_id, folder)
    result = attachments + inline_images

    # PPT/PPTX 转换为 PDF
    converted = []
    for filename, content_type, data in result:
        if filename.lower().endswith((".ppt", ".pptx")):
            pdf_data = convert_ppt_to_pdf(data, filename)
            # 转换成功且生成了 PDF
            if pdf_data != data:
                converted.append((filename.rsplit(".", 1)[0] + ".pdf", "application/pdf", pdf_data))
            else:
                converted.append((filename, content_type, data))
        else:
            converted.append((filename, content_type, data))

    return converted


# ============ 工具函数 ============


def decode_filename(filename) -> str:
    """
    解码邮件中的文件名（支持 RFC 2047 编码）

    Args:
        filename: 原始文件名

    Returns:
        解码后的文件名
    """
    if not filename:
        return ""

    decoded_parts = decode_header(filename)
    result = []

    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            charset = charset or "utf-8"
            try:
                result.append(part.decode(charset))
            except (UnicodeDecodeError, LookupError):
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(part)

    return "".join(result)


def get_extension_from_mime(content_type: str) -> str:
    """
    根据 MIME 类型获取文件扩展名

    Args:
        content_type: MIME 类型

    Returns:
        扩展名（包括点号）
    """
    ext_map = {
        # 图片
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
        "image/tiff": ".tiff",
        "image/heic": ".heic",
        "image/heif": ".heif",
        # 文档
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        # 文本
        "text/plain": ".txt",
        "text/html": ".html",
        "text/csv": ".csv",
        # 压缩文件
        "application/zip": ".zip",
        "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z",
        "application/x-tar": ".tar",
        "application/gzip": ".gz",
    }
    return ext_map.get(content_type.lower(), "")


# ============ PPT/PPTX 转 PDF ============


def convert_ppt_to_pdf(ppt_data: bytes, filename: str) -> bytes:
    """
    将 PPT/PPTX 文件转换为 PDF

    Args:
        ppt_data: PPT/PPTX 文件的字节数据
        filename: 原始文件名（用于确定格式）

    Returns:
        转换后的 PDF 字节数据，转换失败时返回原始 PPT 数据
    """
    temp_dir = None
    try:
        # 创建临时目录
        temp_dir = tempfile.mkdtemp()
        input_path = os.path.join(temp_dir, filename)
        output_path = os.path.join(temp_dir, os.path.splitext(filename)[0] + ".pdf")

        # 写入输入文件
        with open(input_path, "wb") as f:
            f.write(ppt_data)

        # 调用 LibreOffice headless 转换为 PDF
        # --headless: 无GUI模式
        # --convert-to pdf: 转换为PDF
        # --outdir: 输出目录
        result = subprocess.run(
            [
                "soffice",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                temp_dir,
                input_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            print(f"LibreOffice 转换失败: {result.stderr}")
            return ppt_data

        # 读取输出文件
        if os.path.exists(output_path):
            with open(output_path, "rb") as f:
                return f.read()
        else:
            print(f"LibreOffice 未生成输出文件: {output_path}")
            return ppt_data

    except subprocess.TimeoutExpired:
        print("LibreOffice 转换超时（120秒）")
        return ppt_data
    except Exception as e:
        print(f"PPT 转 PDF 转换失败: {e}")
        return ppt_data
    finally:
        # 清理临时目录
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

import imaplib
import os
import re
import threading
import time
from email.header import decode_header
from typing import Any, Dict

from dotenv import load_dotenv
from imap_tools.imap_utf7 import utf7_decode as _utf7_decode

load_dotenv()

# ============ 全局连接管理 ============
_imap_connection: Dict[str, Any] = {"mail": None, "last_used": None}
_connection_lock = threading.Lock()


def get_sync_connection():
    """
    获取独立IMAP连接（sync线程专用，不与API handler共享）

    每次调用都创建全新连接，用完后自己管理生命周期。
    """
    print(f"[{time.strftime('%H:%M:%S')}] [sync] 建立独立IMAP连接...")
    mail = connect_mail()
    print(f"[{time.strftime('%H:%M:%S')}] [sync] IMAP连接已建立")
    return mail


def get_connection():
    """
    获取IMAP连接（复用已有连接）

    如果连接不存在或已断开，则创建新连接
    """
    with _connection_lock:
        mail = _imap_connection.get("mail")

        # 检查连接是否有效
        if mail is not None:
            try:
                mail.noop()  # 检查连接
                _imap_connection["last_used"] = time.time()
                return mail
            except Exception:
                mail = None

        # 创建新连接
        print(f"[{time.strftime('%H:%M:%S')}] 建立IMAP连接...")
        try:
            mail = connect_mail()
            _imap_connection["mail"] = mail
            _imap_connection["last_used"] = time.time()
            print(f"[{time.strftime('%H:%M:%S')}] IMAP连接已建立")
            return mail
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] IMAP连接失败: {e}")
            raise


def invalidate_connection():
    """强制失效当前连接，下次调用get_connection时创建新连接"""
    with _connection_lock:
        mail = _imap_connection.get("mail")
        if mail:
            try:
                mail.logout()
            except Exception:
                pass
        _imap_connection["mail"] = None


def connect_mail():
    """连接邮箱"""
    server = os.getenv("IMAP_SERVER", "imap.263.net")
    port = int(os.getenv("IMAP_PORT", "993"))

    # 根据端口选择 SSL 或普通连接
    if port == 993:
        mail = imaplib.IMAP4_SSL(server)
    else:
        mail = imaplib.IMAP4(server, port)

    mail.socket().settimeout(60)
    mail.login(os.getenv("EMAIL_ACCOUNT", ""), os.getenv("EMAIL_PASSWORD", ""))
    try:
        _, caps = mail.capability()
        if b"UTF8=ACCEPT" in caps[0]:
            mail.enable("UTF8=ACCEPT")
    except Exception:
        pass
    return mail


def decode_imap_utf7(s):
    """
    解码IMAP UTF-7文件夹名

    使用 imap_tools.imap_utf7.utf7_decode 进行正确解码
    """
    if not s:
        return s
    return _utf7_decode(s.encode("utf-8"))


def parse_folder_name(folder_bytes):
    """解析文件夹名"""
    folder_str = folder_bytes.decode("utf-8")
    match = re.search(r'/"([^"]+)"$', folder_str)
    if match:
        return match.group(1)
    parts = folder_str.split('"')
    return parts[-2] if len(parts) >= 2 else folder_str


def quote_folder_name(folder_name: str) -> str:
    """
    引用文件夹名（用于 IMAP 命令）

    如果文件夹名包含空格或特殊字符，需要用双引号包裹
    IMAP Modified UTF-7 编码的文件夹名通常不需要额外引用
    """
    # 检查是否需要引用
    needs_quote = any(c in folder_name for c in ' ()"\\')
    if needs_quote:
        # 转义内部的引号
        escaped = folder_name.replace('"', '\\"')
        return f'"{escaped}"'
    return folder_name


def get_folder_uids(mail) -> list:
    """
    获取文件夹中所有邮件的 UID 列表

    Args:
        mail: 已连接的 IMAP 连接

    Returns:
        UID 字符串列表，如 ['12345', '12346', '12347']
    """
    status, data = mail.uid("SEARCH", None, "ALL")
    if status != "OK" or not data or not data[0]:
        return []
    # 返回字符串列表
    return [uid.decode() if isinstance(uid, bytes) else str(uid) for uid in data[0].split()]


def decode_header_value(value):
    """解码邮件头"""
    if not value:
        return ""
    decoded_parts = []
    for part, encoding in decode_header(value):
        if encoding:
            try:
                decoded_parts.append(part.decode(encoding))
            except (UnicodeDecodeError, LookupError):
                decoded_parts.append(str(part) if isinstance(part, bytes) else part)
        else:
            decoded_parts.append(str(part) if isinstance(part, bytes) else part)
    return "".join(decoded_parts)

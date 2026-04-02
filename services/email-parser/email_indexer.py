"""
邮件索引管理 - SQLite本地索引

功能：
- 管理邮件标题索引
- 增量同步IMAP邮件
- 快速本地搜索
"""

import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

from imap_tools.imap_utf7 import utf7_decode

import mail_client

SKIP_FOLDER_PREFIXES = (
    "&kBp35Q-",  # 通知文件夹
    "&j8dfgIuiU1U-",  # 过往订单
    "Delta&ThpSoQ-/VMT &U1WLwQ- &kBp35Q-",  # Delta业务/VMT 单证 通知
)


def _should_skip_folder(folder_name: str) -> bool:
    """检查是否应该跳过该文件夹"""
    return any(folder_name.startswith(prefix) for prefix in SKIP_FOLDER_PREFIXES)


CHINA_TZ = timezone(timedelta(hours=8))


def _parse_date_to_china_tz(date_str: str) -> Optional[datetime]:
    """解析邮件日期并转换为+8时区后返回 naive datetime"""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CHINA_TZ).replace(tzinfo=None)
    except Exception:
        return None


class EmailIndexer:
    """邮件索引管理器"""

    def __init__(
        self,
        db_path: str = os.getenv("DATABASE_PATH", "/data/email_index.db"),
    ):
        self.db_path = db_path
        # 确保数据库文件所在的目录存在
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """初始化数据库（只在表不存在时创建）"""
        conn = self._get_connection()
        cursor = conn.cursor()

        # 创建索引表（msg_id 现在明确是 UID）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS email_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT NOT NULL,              -- UID（永久唯一标识）
                folder TEXT NOT NULL,              -- 文件夹
                subject TEXT NOT NULL,             -- 邮件标题
                subject_lower TEXT NOT NULL,        -- 小写版本（用于搜索）
                from_addr TEXT,                     -- 发件人
                date DATETIME,                      -- 邮件日期
                indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(folder, msg_id)
            )
        """)

        # 创建索引加速搜索
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_subject_lower
            ON email_index(subject_lower)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_folder
            ON email_index(folder)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_indexed_at
            ON email_index(indexed_at)
        """)

        # 创建文件夹同步状态表（存储 UID 列表用于检测新增/删除）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS folder_sync_meta (
                folder TEXT PRIMARY KEY,            -- 文件夹名
                last_sync DATETIME,                 -- 上次同步时间
                last_uids TEXT,                     -- 上次同步的所有 UID（逗号分隔）
                indexed_count INTEGER DEFAULT 0     -- 已索引的邮件数
            )
        """)

        conn.commit()
        conn.close()

    def _normalize_for_match(self, text: str) -> str:
        """
        规范化标题用于匹配：去除空格、标点、转为小写

        例如: "Q4/ 2026- Stationery" → "q42026stationery"
        """
        import re

        # 不可去除前缀，如 "Re:", "Fw:" 等，用于区分邮件
        # 只保留字母和数字
        text = re.sub(r"[^a-z0-9]", "", text.lower())
        return text

    def _extract_keywords(self, text: str) -> Tuple[Dict[str, int], List[str]]:
        """
        提取关键词（保留前缀及其数量）

        Returns:
            Tuple of (prefix_counts, other_keywords)
            - prefix_counts: dict like {'re': 2, 'aw': 1} for prefix counts
            - other_keywords: list of non-prefix keywords
        """
        import re

        # 1. 提取前缀及其数量
        prefix_patterns = re.findall(r"\b(re|aw|fw|fwd)\b", text.lower())
        prefix_counts: Dict[str, int] = {}
        for p in prefix_patterns:
            prefix_counts[p] = prefix_counts.get(p, 0) + 1

        # 2. 去除前缀后的文本
        text_without_prefixes = re.sub(r"\b(re|aw|fw|fwd)\b", "", text.lower())

        # 3. 提取其他关键词（3个字母以上）
        words = re.findall(r"[a-z]{3,}", text_without_prefixes)
        # 去重但保留顺序
        seen = set()
        other_keywords = []
        for w in words:
            if w not in seen:
                seen.add(w)
                other_keywords.append(w)

        # 限制总数
        other_keywords = other_keywords[:5]

        return prefix_counts, other_keywords

    def _extract_prefix_seq_from_subject(self, subject_lower: str) -> str:
        """从 subject_lower 提取前缀序列"""
        import re

        prefixes = re.findall(r"\b(re|aw|fw|fwd)\b", subject_lower)
        return "".join(prefixes)

    def _extract_keywords_for_match(self, subject: str) -> List[str]:
        """
        提取用于匹配的关键词

        例如: "Re: AW: AW: Q2/2027 Mini Camera" → ['q2', '2027', 'mini', 'camera']
        """
        import re

        text = subject.lower()

        # 去除常见前缀
        text = re.sub(r"^(re|aw|fw|fwd)[:\s]+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^(回复|转发)[:\s]*", "", text)

        keywords = []

        # 提取 Q+数字 模式（如 q2, q2027）
        q_patterns = re.findall(r"q\d+(?:/\d+)?", text)
        keywords.extend(q_patterns)

        # 提取纯数字年份
        year_patterns = re.findall(r"\b(20\d{2})\b", text)
        keywords.extend(year_patterns)

        # 提取其他关键词（3个字母以上或产品型号）
        words = re.findall(r"[a-z]{3,}|\d+[a-z]+\d+|[a-z]+\d+[a-z]+", text)
        keywords.extend(words)

        # 去重
        return list(dict.fromkeys(keywords))

    def search_by_subject(self, title: str) -> Optional[Dict]:
        """
        根据标题搜索邮件（精确匹配 + 模糊匹配保底）

        Args:
            title: 邮件标题

        Returns:
            邮件信息字典，如果没找到返回None
        """
        subject_lower = title.strip().lower()

        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 1. 精确匹配（按时间倒序，取最新的）
            cursor.execute(
                """SELECT * FROM email_index
                   WHERE subject_lower = ?
                   ORDER BY date DESC
                   LIMIT 1""",
                (subject_lower,),
            )
            row = cursor.fetchone()

            # 2. 模糊匹配保底：用关键词 AND 匹配
            if not row:
                keywords = self._extract_keywords_for_match(title)
                if keywords:
                    conditions = " AND ".join(
                        ["subject_lower LIKE ?" for _ in keywords]
                    )
                    params = [f"%{kw}%" for kw in keywords]
                    cursor.execute(
                        f"""SELECT * FROM email_index
                           WHERE {conditions}
                           ORDER BY date DESC
                           LIMIT 20""",
                        params,
                    )
                    rows = list(cursor.fetchall())
                    if rows:
                        row = rows[0]

            conn.close()

            if row:
                columns = [
                    "id",
                    "msg_id",
                    "folder",
                    "subject",
                    "subject_lower",
                    "from_addr",
                    "date",
                    "indexed_at",
                ]
                return dict(zip(columns, row))

            return None

    def search_by_keywords(self, keywords: List[str]) -> List[Dict]:
        """
        根据关键词搜索邮件（模糊匹配任一关键词）

        Args:
            keywords: 关键词列表

        Returns:
            匹配的邮件列表
        """
        if not keywords:
            return []

        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            conditions = " OR ".join(["subject_lower LIKE ?" for _ in keywords])
            params = [f"%{kw.lower()}%" for kw in keywords]

            cursor.execute(
                f"""SELECT * FROM email_index
                   WHERE {conditions}
                   ORDER BY date DESC
                   LIMIT 50""",
                params,
            )

            rows = cursor.fetchall()
            conn.close()

            return [dict(row) for row in rows]

    def add_or_update(
        self,
        msg_id: str,
        folder: str,
        subject: str,
        from_addr: str = None,
        date: datetime = None,
    ):
        """添加或更新邮件索引"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(
                """
                INSERT OR REPLACE INTO email_index
                (msg_id, folder, subject, subject_lower, from_addr, date, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    msg_id,
                    folder,
                    subject,
                    subject.lower(),
                    from_addr,
                    date,
                    datetime.now(),
                ),
            )

            conn.commit()
            conn.close()

    def delete_by_folder(self, folder: str):
        """删除指定文件夹的所有索引"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM email_index WHERE folder = ?", (folder,))
            conn.commit()
            conn.close()

    def get_all_folders(self) -> List[str]:
        """获取所有已索引的文件夹（包括只有同步记录的文件夹）"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            # 同时查询 email_index 和 folder_sync_meta，避免遗漏空文件夹
            cursor.execute(
                "SELECT folder FROM email_index UNION SELECT folder FROM folder_sync_meta"
            )
            folders = [row[0] for row in cursor.fetchall()]
            conn.close()
            return folders

    def get_stats(self) -> Dict:
        """获取索引统计信息"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM email_index")
            total = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(DISTINCT folder) FROM email_index")
            folders = cursor.fetchone()[0]

            cursor.execute("SELECT MAX(indexed_at) FROM email_index")
            last_sync = cursor.fetchone()[0]

            conn.close()

            return {
                "total_emails": total,
                "total_folders": folders,
                "last_sync": last_sync,
            }

    def get_folder_sync_meta(self, folder: str) -> Optional[Dict]:
        """获取指定文件夹的同步状态"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT last_sync, last_uids, indexed_count FROM folder_sync_meta WHERE folder = ?",
                (folder,),
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "last_sync": datetime.fromisoformat(row[0]) if row[0] else None,
                    "last_uids": row[1],
                    "indexed_count": row[2],
                }
            return None

    def update_folder_sync_meta(
        self,
        folder: str,
        sync_time: datetime,
        last_uids: str = None,
        indexed_count: int = 0,
    ):
        """更新指定文件夹的同步状态"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO folder_sync_meta (folder, last_sync, last_uids, indexed_count) VALUES (?, ?, ?, ?)",
                (folder, sync_time.isoformat(), last_uids, indexed_count),
            )
            conn.commit()
            conn.close()

    def delete(self, folder: str, msg_id: str):
        """从索引中删除指定邮件"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM email_index WHERE folder = ? AND msg_id = ?",
                (folder, msg_id),
            )
            conn.commit()
            conn.close()

    def delete_folder(self, folder: str):
        """删除文件夹的所有索引记录"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM email_index WHERE folder = ?", (folder,))
            cursor.execute("DELETE FROM folder_sync_meta WHERE folder = ?", (folder,))
            conn.commit()
            conn.close()


# 全局索引实例
_indexer: Optional[EmailIndexer] = None
_sync_lock = threading.Lock()


def _iter_by_uids(mail, msg_count: int, batch_size: int = 500):
    """
    按消息数量获取邮件头部信息（生成器模式）

    使用序列号 FETCH 获取数据，从响应中提取真正的 UID

    Args:
        mail: IMAP连接
        msg_count: 邮件总数
        batch_size: 每批获取的数量，默认100

    Yields:
        tuple: (uid, header_info_dict)
    """
    if msg_count <= 0:
        return

    for start in range(1, msg_count + 1, batch_size):
        end = min(start + batch_size - 1, msg_count)
        seq_range = f"{start}:{end}"

        try:
            # 使用序列号 FETCH，同时获取 UID 和头部
            status, data = mail.fetch(
                seq_range, "(UID BODY[HEADER.FIELDS (SUBJECT FROM DATE)])"
            )
            if status != "OK" or not data:
                continue

            for item in data:
                if not item:
                    continue

                if isinstance(item, tuple) and len(item) >= 2:
                    resp_text = (
                        item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                    )
                    header_text = (
                        item[1].decode() if isinstance(item[1], bytes) else str(item[1])
                    )
                else:
                    resp_text = str(item)
                    header_text = ""

                # 从响应中提取 UID
                uid_match = re.search(r"UID (\d+)", resp_text)
                if not uid_match:
                    continue
                actual_uid = uid_match.group(1)

                # 提取头部字段
                subject = ""
                from_addr = ""
                date_str = ""

                subj_match = re.search(
                    r"Subject:\s*([^\r\n]+)", header_text, re.IGNORECASE
                )
                if subj_match:
                    subject = subj_match.group(1).strip()

                from_match = re.search(
                    r"From:\s*([^\r\n]+)", header_text, re.IGNORECASE
                )
                if from_match:
                    from_addr = from_match.group(1).strip()

                date_match = re.search(
                    r"Date:\s*([^\r\n]+)", header_text, re.IGNORECASE
                )
                if date_match:
                    date_str = date_match.group(1).strip()

                yield (
                    actual_uid,
                    {"subject": subject, "from_addr": from_addr, "date": date_str},
                )

        except Exception as e:
            print(f"批量获取头部失败: {e}")
            continue


def get_indexer() -> EmailIndexer:
    """获取全局索引实例"""
    global _indexer
    if _indexer is None:
        _indexer = EmailIndexer()
    return _indexer


def sync_incremental(mail, dedicated: bool = False) -> Dict:
    """
    增量同步 - 按文件夹跟踪各自的上次同步状态

    处理：
    - 新增文件夹：自动全量同步
    - 删除文件夹：从索引中移除
    - 新增邮件：按 UID 添加到索引
    - 删除邮件：按 UID 从索引移除

    Args:
        mail: 已连接的IMAP连接

    Returns:
        同步统计信息
    """
    indexer = get_indexer()
    stats = {
        "folders_scanned": 0,
        "folders_added": 0,
        "folders_deleted": 0,
        "emails_indexed": 0,
        "emails_deleted": 0,
        "folders_with_changes": 0,
        "errors": [],
        "mode": "incremental",
    }

    try:
        status, folders = mail.list("", "*")
        if status != "OK":
            stats["errors"].append("获取文件夹列表失败")
            return stats

        # 获取服务器上的所有文件夹
        server_folders = {}
        for folder_data in folders:
            folder_name = mail_client.parse_folder_name(folder_data)
            server_folders[folder_name] = folder_data

        # 显式检查并同步 INBOX（mail.list 不返回 INBOX）
        # 必须在计算 deleted_folders 之前执行，否则 INBOX 会被错误地标记为删除
        inbox_name = "INBOX"
        try:
            status, data = mail.select("INBOX")
            if status == "OK":
                inbox_uids = mail_client.get_folder_uids(mail)
                print(
                    f"[DEBUG] INBOX 存在，共 {len(inbox_uids)} 封邮件",
                    flush=True,
                )
                # 立即将 INBOX 加入 server_folders，避免被误判为已删除
                if inbox_name not in server_folders:
                    server_folders[inbox_name] = None
            else:
                print("[DEBUG] INBOX 不存在或无法访问", flush=True)
        except Exception as e:
            print(f"[DEBUG] 检查 INBOX 失败: {e}", flush=True)

        # 获取索引中已有的所有文件夹
        indexed_folders = set(indexer.get_all_folders())
        server_folder_names = set(server_folders.keys())

        # 计算新增和删除的文件夹
        new_folders = server_folder_names - indexed_folders
        deleted_folders = indexed_folders - server_folder_names

        # INBOX 未索引时，添加到新增列表
        if inbox_name not in indexed_folders and inbox_name in server_folders:
            new_folders.add(inbox_name)

        print(
            f"[DEBUG] 服务器文件夹: {len(server_folder_names)}, 索引文件夹: {len(indexed_folders)}",
            flush=True,
        )
        print(
            f"[DEBUG] 新增文件夹: {len(new_folders)}, 删除文件夹: {len(deleted_folders)}",
            flush=True,
        )

        # 处理新增文件夹 - 自动全量同步
        for folder_name in new_folders:
            if _should_skip_folder(folder_name):
                continue
            try:
                folder_display = utf7_decode(folder_name.encode("utf-8"))
            except Exception:
                folder_display = folder_name

            print(f"  [新增文件夹] {folder_display} - 开始全量同步", flush=True)
            try:
                status, data = mail.select(mail_client.quote_folder_name(folder_name))
                if status != "OK":
                    print(
                        f"    [ERROR] mail.select 失败: status={status}, data={data}, folder={folder_display}"
                    )
                    stats["errors"].append(
                        f"{folder_display}: select失败 status={status}"
                    )
                    continue

                current_uids = mail_client.get_folder_uids(mail)
                if not current_uids:
                    # 空文件夹也记录到索引，避免下次重复打印
                    indexer.update_folder_sync_meta(
                        folder=folder_name,
                        sync_time=datetime.now(),
                        last_uids="",
                        indexed_count=0,
                    )
                    stats["folders_added"] += 1
                    stats["folders_scanned"] += 1
                    continue

                folder_indexed = 0
                for uid, header_info in _iter_by_uids(mail, len(current_uids)):
                    try:
                        subject = mail_client.decode_header_value(
                            header_info.get("subject", "")
                        )
                        from_addr = mail_client.decode_header_value(
                            header_info.get("from_addr", "")
                        )
                        date_str = header_info.get("date", "")

                        msg_date = _parse_date_to_china_tz(date_str)

                        indexer.add_or_update(
                            msg_id=uid,
                            folder=folder_name,
                            subject=subject,
                            from_addr=from_addr,
                            date=msg_date,
                        )
                        folder_indexed += 1
                        stats["emails_indexed"] += 1
                    except Exception:
                        pass

                # 更新文件夹同步状态
                indexer.update_folder_sync_meta(
                    folder=folder_name,
                    sync_time=datetime.now(),
                    last_uids=",".join(current_uids),
                    indexed_count=folder_indexed,
                )
                stats["folders_added"] += 1
                stats["folders_scanned"] += 1
                print(f"    完成: {folder_indexed} 封", flush=True)

            except Exception as e:
                stats["errors"].append(f"新增文件夹 {folder_display}: {str(e)}")

        # 处理删除文件夹 - 从索引中移除
        for folder_name in deleted_folders:
            try:
                indexer.delete_folder(folder_name)
                stats["folders_deleted"] += 1
                print(f"  [删除文件夹] {folder_name}", flush=True)
            except Exception as e:
                stats["errors"].append(f"删除文件夹 {folder_name}: {str(e)}")

        # 继续处理已存在的文件夹（增量同步）
        print("[DEBUG] 开始增量同步已有文件夹...", flush=True)
        checked_count = 0

        # 将 INBOX 添加到增量同步列表（如果已索引但不在 folders 中）
        if inbox_name in indexed_folders and inbox_name not in new_folders:
            # 构造一个假的 folder_data 让循环能处理 INBOX
            # 格式是 mail.list 返回的 (status, [b'(\\Marked \\HasNoChildren) "/" INBOX'])
            folders_for_incremental = folders + [None]  # None 表示需要特殊处理
            folder_name_map = {
                i: mail_client.parse_folder_name(f) for i, f in enumerate(folders)
            }
            folder_name_map[len(folders)] = inbox_name
        else:
            folders_for_incremental = folders
            folder_name_map = {
                i: mail_client.parse_folder_name(f) for i, f in enumerate(folders)
            }

        for i, folder_data in enumerate(folders_for_incremental):
            folder_name = folder_name_map[i]
            checked_count += 1

            # 跳过已处理的新增文件夹
            if folder_name in new_folders:
                continue

            # 跳过指定文件夹
            if _should_skip_folder(folder_name):
                continue

            # 每 50 个文件夹打印一次进度
            if checked_count % 50 == 0:
                print(
                    f"[DEBUG] 已检查 {checked_count}/{len(folders_for_incremental)} 个文件夹...",
                    flush=True,
                )

            try:
                folder_display = utf7_decode(folder_name.encode("utf-8"))
            except Exception:
                folder_display = folder_name

            # 检查该文件夹的上次同步状态
            folder_meta = indexer.get_folder_sync_meta(folder_name)
            if not folder_meta:
                # 新文件夹（从未全量同步过），跳过（理论上不会出现，因为已在上面处理）
                continue

            try:
                # 重试循环：连接断开后会重新 select 该文件夹
                for attempt in range(2):
                    status, data = mail.select(mail_client.quote_folder_name(folder_name))
                    if status == "OK":
                        break
                    # select 失败，换新连接后重试同文件夹
                    if dedicated:
                        mail = mail_client.get_sync_connection()
                    else:
                        mail_client.invalidate_connection()
                        mail = mail_client.get_connection()
                else:
                    # 两次都失败，记录错误后跳过该文件夹
                    stats["errors"].append(f"{folder_display}: select失败，跳过")
                    continue

                # 获取当前所有 UID
                current_uids = set(mail_client.get_folder_uids(mail))

                # 解析上次的 UID 列表
                last_uids_str = folder_meta.get("last_uids") or ""
                last_uids = set(last_uids_str.split(",")) if last_uids_str else set()

                # 计算差异
                new_uids = current_uids - last_uids  # 新增的
                gone_uids = last_uids - current_uids  # 删除/移动走的

                # 如果有变化才处理
                if new_uids or gone_uids:
                    stats["folders_with_changes"] += 1
                    print(
                        f"  [{folder_display[:35]:<35}] 新增:{len(new_uids)} 删除:{len(gone_uids)}"
                    )

                    # 批量获取所有邮件，通过 UID 过滤只处理新增的
                    # msg_count = 邮件总数，即序列号范围 1:msg_count
                    for uid, header_info in _iter_by_uids(mail, len(current_uids)):
                        if uid not in new_uids:
                            continue
                        try:
                            subject = mail_client.decode_header_value(
                                header_info.get("subject", "")
                            )
                            from_addr = mail_client.decode_header_value(
                                header_info.get("from_addr", "")
                            )
                            date_str = header_info.get("date", "")

                            msg_date = _parse_date_to_china_tz(date_str)

                            indexer.add_or_update(
                                msg_id=uid,
                                folder=folder_name,
                                subject=subject,
                                from_addr=from_addr,
                                date=msg_date,
                            )
                            stats["emails_indexed"] += 1
                        except Exception as e:
                            print(f"    添加邮件 UID {uid} 失败: {e}")

                    # 处理删除的邮件（从索引中移除）
                    for uid in gone_uids:
                        try:
                            indexer.delete(folder_name, uid)
                            stats["emails_deleted"] += 1
                        except Exception as e:
                            print(f"    删除邮件 UID {uid} 失败: {e}")

                # 更新文件夹同步状态（存储所有 UID）
                indexer.update_folder_sync_meta(
                    folder=folder_name,
                    sync_time=datetime.now(),
                    last_uids=",".join(current_uids),
                    indexed_count=len(current_uids),
                )

                stats["folders_scanned"] += 1

            except Exception as e:
                stats["errors"].append(f"{folder_display}: {e}")
                print(f"    文件夹 {folder_display} 处理失败: {e}")

        print(
            f"[DEBUG] 增量同步完成: "
            f"新增文件夹{stats['folders_added']}个, "
            f"删除文件夹{stats['folders_deleted']}个, "
            f"新增邮件{stats['emails_indexed']}封, "
            f"删除邮件{stats['emails_deleted']}封, "
            f"{stats['folders_with_changes']}个已有文件夹有变化"
        )

        if stats["errors"]:
            print(f"[DEBUG] 同步错误列表 ({len(stats['errors'])} 条):")
            for err in stats["errors"]:
                print(f"  - {err}")

    except Exception as e:
        stats["errors"].append(f"同步失败: {str(e)}")
        print(f"增量同步失败: {e}")

    return stats


def sync_all_folders(mail) -> Dict:
    """
    同步所有文件夹的邮件到索引（全量同步）

    使用 UID 获取所有邮件，并记录每个文件夹的所有 UID

    Args:
        mail: 已连接的IMAP连接

    Returns:
        同步统计信息
    """
    indexer = get_indexer()
    stats = {
        "folders_scanned": 0,
        "emails_indexed": 0,
        "emails_deleted": 0,
        "errors": [],
    }

    try:
        # 获取所有文件夹 (包括嵌套文件夹)
        status, folders = mail.list("", "*")
        if status != "OK":
            stats["errors"].append("获取文件夹列表失败")
            return stats

        print(f"获取到 {len(folders)} 个文件夹，开始全量同步...")

        for folder_data in folders:
            folder_name = mail_client.parse_folder_name(folder_data)
            if _should_skip_folder(folder_name):
                continue
            try:
                folder_display = utf7_decode(folder_name.encode("utf-8"))
            except Exception:
                folder_display = folder_name

            try:
                status, data = mail.select(mail_client.quote_folder_name(folder_name))
                if status != "OK":
                    stats["errors"].append(f"选择文件夹失败: {folder_display}")
                    if dedicated:
                        mail = mail_client.get_sync_connection()
                    else:
                        mail_client.invalidate_connection()
                        mail = mail_client.get_connection()
                    continue

                # 获取当前所有 UID
                current_uids = mail_client.get_folder_uids(mail)

                if not current_uids:
                    continue

                stats["folders_scanned"] += 1
                print(f"正在同步: {folder_display} ({len(current_uids)} 封)")

                # 使用 _iter_by_uids 获取所有邮件（通过序列号 FETCH 提取真实 UID）
                indexed = 0
                for uid, header_info in _iter_by_uids(mail, len(current_uids)):
                    try:
                        subject = mail_client.decode_header_value(
                            header_info.get("subject", "")
                        )
                        from_addr = mail_client.decode_header_value(
                            header_info.get("from_addr", "")
                        )
                        date_str = header_info.get("date", "")

                        msg_date = _parse_date_to_china_tz(date_str)

                        indexer.add_or_update(
                            msg_id=uid,
                            folder=folder_name,
                            subject=subject,
                            from_addr=from_addr,
                            date=msg_date,
                        )
                        indexed += 1
                        stats["emails_indexed"] += 1

                        if indexed % 100 == 0:
                            print(
                                f"  [{folder_display[:25]:<25}] {indexed}/{len(current_uids)}"
                            )

                    except Exception:
                        pass

                # 同步完成后更新文件夹状态（存储所有 UID）
                indexer.update_folder_sync_meta(
                    folder=folder_name,
                    sync_time=datetime.now(),
                    last_uids=",".join(current_uids),
                    indexed_count=indexed,
                )

                print(f"  完成: {indexed}封")

            except Exception as e:
                stats["errors"].append(f"文件夹 {folder_display}: {str(e)}")
                print(f"同步文件夹 {folder_display} 出错: {e}")

    except Exception as e:
        stats["errors"].append(f"同步失败: {str(e)}")
        print(f"同步失败: {e}")

    return stats

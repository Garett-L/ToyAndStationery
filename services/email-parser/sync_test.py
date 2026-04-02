#!/usr/bin/env python3
"""
独立索引同步脚本

功能：
- 清除旧索引并重建
- 分批获取所有文件夹的邮件头部
- 显示同步进度条
- 完成后显示索引统计
- 断点续传：跳过已完整同步的文件夹

用法：
    python sync_test.py              # 全量同步
    python sync_test.py --stats     # 仅查看统计
    python sync_test.py --clear     # 清除索引后同步
    python sync_test.py --continue  # 断点续传（跳过已同步文件夹）
"""

import os
import sys
import time

# 添加项目路径
sys.path.insert(0, ".")

import email_indexer
import mail_client


class ProgressBar:
    """简单的进度条"""

    def __init__(self, total, width=40, desc=""):
        self.total = total
        self.width = width
        self.desc = desc
        self.current = 0

    def update(self, n=1):
        self.current += n
        self.draw()

    def draw(self):
        if self.total == 0:
            percent = 100
            filled = self.width
        else:
            percent = min(100, self.current / self.total * 100)
            filled = int(self.width * percent / 100)

        bar = "=" * filled + "-" * (self.width - filled)
        sys.stdout.write(
            f"\r{self.desc} [{bar}] {self.current}/{self.total} ({percent:.1f}%)"
        )
        sys.stdout.flush()

    def finish(self):
        self.current = self.total
        self.draw()
        sys.stdout.write("\n")
        sys.stdout.flush()


def clear_index():
    """清除旧索引"""
    import os

    db_path = os.getenv("DATABASE_PATH", "/data/email_index.db")
    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"已删除旧索引: {db_path}")
    else:
        print("没有找到旧索引")


def sync_all_with_progress(continue_mode=False):
    """
    带进度条显示的同步

    Args:
        continue_mode: True 则跳过已完整同步的文件夹（断点续传）
    """
    print("=" * 60)
    if continue_mode:
        print("邮件索引同步 (断点续传模式)")
    else:
        print("邮件索引同步")
    print("=" * 60)

    # 获取 IMAP 连接
    print("\n[1/4] 连接 IMAP 服务器...")
    mail = mail_client.get_connection()

    # 获取所有文件夹
    print("[2/4] 获取文件夹列表...")
    status, folders = mail.list("", "*")
    if status != "OK":
        print(f"获取文件夹列表失败: {status}")
        return

    print(f"  服务器共有 {len(folders)} 个文件夹")

    # 开始同步
    print("[3/4] 开始同步邮件...\n")
    start_time = time.time()

    indexer = email_indexer.get_indexer()
    stats = {"folders_scanned": 0, "emails_indexed": 0, "errors": []}

    for folder_data in folders:
        folder_name = mail_client.parse_folder_name(folder_data)
        try:
            folder_display = mail_client.decode_imap_utf7(folder_name)
        except Exception:
            folder_display = folder_name

        # 跳过通知文件夹和过往订单文件夹和 Delta业务/VMT 单证 通知文件夹
        if (
            folder_name.startswith("&kBp35Q-")
            or folder_name.startswith("&j8dfgIuiU1U-")
            or folder_name.startswith("Delta&ThpSoQ-/VMT &U1WLwQ- &kBp35Q-")
        ):
            continue

        try:
            status, data = mail.select(mail_client.quote_folder_name(folder_name))
            if status != "OK":
                continue

            # 获取所有 UID
            uids = mail_client.get_folder_uids(mail)
            if not uids:
                continue

            # 断点续传模式：检查该文件夹是否已完整同步
            if continue_mode:
                need_sync, reason, _ = check_folder_need_sync(folder_name, len(uids))
                if not need_sync:
                    print(f"  [跳过] {folder_display} - {reason}")
                    continue
                else:
                    print(f"  [继续] {folder_display} - {reason}")

            stats["folders_scanned"] += 1

            # 创建进度条
            bar = ProgressBar(len(uids), width=40, desc=folder_display[:35])
            indexed = 0

            for uid, header_info in email_indexer._iter_by_uids(mail, len(uids)):
                try:
                    subject = mail_client.decode_header_value(
                        header_info.get("subject", "")
                    )
                    from_addr = mail_client.decode_header_value(
                        header_info.get("from_addr", "")
                    )
                    date_str = header_info.get("date", "")

                    msg_date = None
                    if date_str:
                        try:
                            from email.utils import parsedate_to_datetime

                            msg_date = parsedate_to_datetime(date_str)
                        except Exception:
                            pass

                    indexer.add_or_update(
                        msg_id=uid,
                        folder=folder_name,
                        subject=subject,
                        from_addr=from_addr,
                        date=msg_date,
                    )
                    stats["emails_indexed"] += 1
                    indexed += 1
                    bar.update(1)

                except Exception:
                    pass

            bar.finish()

            # 同步完成后更新文件夹状态（存储所有 UID）
            indexer.update_folder_sync_meta(
                folder=folder_name,
                sync_time=email_indexer.datetime.now(),
                last_uids=",".join(uids),
                indexed_count=indexed,
            )

        except Exception as e:
            stats["errors"].append(f"{folder_display}: {e}")
            print(f"[!] 文件夹 {folder_display} 出错: {e}")

    elapsed = time.time() - start_time

    # 显示统计
    print("\n[4/4] 同步完成!")
    print("=" * 60)
    print(f"  扫描文件夹: {stats['folders_scanned']}")
    print(f"  索引邮件数: {stats['emails_indexed']}")
    print(f"  耗时: {elapsed:.1f} 秒")
    if elapsed > 0:
        print(f"  速度: {stats['emails_indexed'] / elapsed:.0f} 封/秒")

    if stats["errors"]:
        print(f"  错误数: {len(stats['errors'])}")

    print("=" * 60)


def show_stats():
    """显示索引统计"""
    import sqlite3

    db_path = os.getenv("DATABASE_PATH", "/data/email_index.db")
    if not os.path.exists(db_path):
        print(f"数据库文件不存在: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("=" * 60)
    print("索引统计")
    print("=" * 60)

    try:
        cursor.execute("SELECT COUNT(*) FROM email_index")
        total = cursor.fetchone()[0]
        print(f"  总邮件数: {total}")

        cursor.execute("SELECT COUNT(DISTINCT folder) FROM email_index")
        folders = cursor.fetchone()[0]
        print(f"  文件夹数: {folders}")

        cursor.execute(
            "SELECT folder, COUNT(*) as cnt FROM email_index GROUP BY folder ORDER BY cnt DESC LIMIT 10"
        )
        print("\n  文件夹详情 (TOP 10):")
        for folder, cnt in cursor.fetchall():
            try:
                display_name = mail_client.decode_imap_utf7(folder)
            except Exception:
                display_name = folder
            print(f"    {cnt:5d} | {display_name[:50]}")

        cursor.execute("SELECT MAX(indexed_at) FROM email_index")
        last_sync = cursor.fetchone()[0]
        print(f"\n  最后同步: {last_sync}")

    except Exception as e:
        print(f"  错误: {e}")

    conn.close()
    print("=" * 60)


def get_folder_sync_status(folder_name: str) -> dict:
    """
    查询文件夹的同步状态

    Returns:
        dict: {
            "exists": bool,           # 是否有过同步记录
            "indexed_count": int,      # 上次索引的邮件数
            "last_uids": set,          # 上次同步的所有 UID
            "last_sync": datetime      # 上次同步时间
        }
    """
    import sqlite3

    db_path = os.getenv("DATABASE_PATH", "/data/email_index.db")
    if not os.path.exists(db_path):
        return {
            "exists": False,
            "indexed_count": 0,
            "last_uids": set(),
            "last_sync": None,
        }

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT last_sync, last_uids, indexed_count FROM folder_sync_meta WHERE folder = ?",
        (folder_name,),
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return {
            "exists": False,
            "indexed_count": 0,
            "last_uids": set(),
            "last_sync": None,
        }

    last_uids = set(row[1].split(",")) if row[1] else set()
    return {
        "exists": True,
        "indexed_count": row[2] or 0,
        "last_uids": last_uids,
        "last_sync": row[0],
    }


def check_folder_need_sync(folder_name: str, server_uid_count: int) -> tuple:
    """
    检查文件夹是否需要同步

    Returns:
        tuple: (need_sync: bool, reason: str, status: dict)
    """
    status = get_folder_sync_status(folder_name)

    if not status["exists"]:
        return True, "新文件夹", status

    if status["indexed_count"] != server_uid_count:
        return (
            True,
            f"数量不一致 (索引:{status['indexed_count']}, 服务器:{server_uid_count})",
            status,
        )

    return False, "已同步", status


def main():
    import argparse

    parser = argparse.ArgumentParser(description="邮件索引同步脚本")
    parser.add_argument("--stats", action="store_true", help="仅显示统计信息")
    parser.add_argument("--clear", action="store_true", help="清除索引后同步")
    parser.add_argument(
        "--continue",
        dest="continue_mode",
        action="store_true",
        help="断点续传：跳过已完整同步的文件夹",
    )
    parser.add_argument(
        "--debug-email",
        dest="debug_email",
        action="store_true",
        help="调试邮件正文提取",
    )
    parser.add_argument(
        "--subject",
        dest="subject",
        type=str,
        help="邮件标题（用于调试）",
    )
    parser.add_argument(
        "--uid",
        dest="uid",
        type=str,
        help="邮件 UID",
    )
    parser.add_argument(
        "--folder",
        dest="folder",
        type=str,
        help="文件夹名",
    )

    args = parser.parse_args()

    if args.debug_email:
        debug_email_body(subject=args.subject, msg_id=args.uid, folder=args.folder)
    elif args.stats:
        show_stats()
    elif args.clear:
        clear_index()
        sync_all_with_progress()
    elif args.continue_mode:
        sync_all_with_progress(continue_mode=True)
    else:
        sync_all_with_progress()


if __name__ == "__main__":
    main()


def debug_email_body(subject: str = None, msg_id: str = None, folder: str = None):
    """
    调试邮件正文提取

    用法:
        python sync_test.py --debug-email "AW: Re: AW: AM2604- Q1/2027 Wooden toys"
        python sync_test.py --debug-email --uid 56 --folder "&i+JO9w-/Delta..."

    参数:
        subject: 邮件标题（用于搜索）
        msg_id: 邮件 UID（直接指定）
        folder: 文件夹名
    """
    import email_parser

    indexer = email_indexer.get_indexer()

    if msg_id and folder:
        email_info = {"msg_id": msg_id, "folder": folder}
    elif subject:
        email_info = indexer.search_by_subject(subject)
        if not email_info:
            print(f"未找到邮件: {subject}")
            return
    else:
        print("请提供 --uid 和 --folder 参数，或 --subject 参数")
        return

    print(f"邮件信息:")
    print(f"  UID: {email_info['msg_id']}")
    print(f"  文件夹: {email_info['folder']}")
    print(f"  主题: {email_info.get('subject', 'N/A')}")
    print()

    # 获取正文
    body = email_parser.get_email_body(email_info["msg_id"], email_info["folder"])

    print(f"正文预览 (前 500 字符):")
    print("-" * 50)
    print(body[:500])
    print("-" * 50)
    print(f"\n正文总长度: {len(body)} 字符")

    # 保存到文件
    safe_subject = "".join(
        c if c.isalnum() else "_" for c in email_info.get("subject", "unknown")[:30]
    )
    output_dir = os.path.dirname(os.getenv("DATABASE_PATH", "/data/debug_output"))
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"debug_email_{safe_subject}.txt")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"\n完整正文已保存到: {output_file}")

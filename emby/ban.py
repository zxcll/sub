import re
import sys
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import subprocess
import requests
import pymysql

def get_public_ip():
    try:
        response = requests.get('https://api.ipify.org', timeout=5)
        return response.text
    except:
        return "Unknown-IP"

def get_db_connection(args):
    try:
        return pymysql.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            db=args.db,
            charset='utf8mb4',
            autocommit=True
        )
    except Exception as e:
        print(f"❌ 数据库连接失败: {e}")
        return None

def init_database(args):
    conn = get_db_connection(args)
    if not conn: return
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS banned_user_stats (
        userid VARCHAR(64) NOT NULL,
        server_name VARCHAR(64),
        request_count INT DEFAULT 0,
        top_ua TEXT,
        local_play_type VARCHAR(64) DEFAULT '',
        cloud_play_type VARCHAR(64) DEFAULT '',
        op_ip VARCHAR(45) DEFAULT '',
        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (userid)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(create_table_sql)
            # 尝试增加 op_ip 字段（如果旧表没有的话）
            try:
                cursor.execute("ALTER TABLE banned_user_stats ADD COLUMN op_ip VARCHAR(45) DEFAULT '' AFTER cloud_play_type")
            except: pass 
    finally:
        conn.close()

def upsert_user_data(args, userid, server, count, ua, op_ip):
    conn = get_db_connection(args)
    if not conn: return
    
    val_local = "Emby本地" if args.mode == 'LOCAL' else ""
    val_cloud = "Google网盘" if args.mode == 'CLOUD' else ""

    sql = """
    INSERT INTO banned_user_stats 
    (userid, server_name, request_count, top_ua, local_play_type, cloud_play_type, op_ip, last_updated)
    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
    ON DUPLICATE KEY UPDATE
        server_name = VALUES(server_name),
        request_count = VALUES(request_count),
        top_ua = VALUES(top_ua),
        local_play_type = VALUES(local_play_type),
        cloud_play_type = VALUES(cloud_play_type),
        op_ip = VALUES(op_ip),
        last_updated = NOW();
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (userid, server, count, ua, val_local, val_cloud, op_ip))
    finally:
        conn.close()

def main():
    parser = argparse.ArgumentParser(description="Emby封禁脚本远程版")
    parser.add_argument("--host", required=True, help="数据库地址")
    parser.add_argument("--port", type=int, default=3306, help="数据库端口")
    parser.add_argument("--user", required=True, help="数据库用户名")
    parser.add_argument("--password", required=True, help="数据库密码")
    parser.add_argument("--db", required=True, help="数据库名")
    parser.add_argument("--log", required=True, help="日志文件路径")
    parser.add_argument("--mode", choices=['LOCAL', 'CLOUD'], default='CLOUD', help="播放模式")
    parser.add_argument("--limit", type=int, default=1800, help="封禁阈值")
    parser.add_argument("--banfile", default="/home/banuserid.txt", help="封禁清单路径")
    parser.add_argument("--chatid", default=None, help="TGID")
    parser.add_argument("--botkey", default=None, help="TGBOTAPI")
    
    args = parser.parse_args()

    init_database(args)
    current_ip = get_public_ip()
    yesterday = datetime.now().strftime("%d/%b/%Y")
    
    print(f"🚀 开始任务 | 模式: {args.mode} | 服务器IP: {current_ip}")

    userid_counter = Counter()
    userid_ua_tracker = defaultdict(Counter)
    userid_server_tracker = {}
    
    # 这里的正则保持不变...
    userid_pattern = re.compile(r"userid=([^&\s]+)")
    server_pattern = re.compile(r"server=([^&\s]+)")
    ua_pattern = re.compile(r'"([^"]*)"\s*$')
    server_map = {'1':'gy','2':'gf','3':'ald','4':'dyy','5':'YKK','6':'qf','7':'momo','8':'mc','9':'zdxb','99':'其他'}

    try:
        with open(args.log, "r", encoding="utf-8", errors='ignore') as f:
            for line in f:
                if yesterday in line:
                    u_match = userid_pattern.search(line)
                    if u_match:
                        uid = u_match.group(1)
                        userid_counter[uid] += 1
                        ua_match = ua_pattern.search(line.strip())
                        if ua_match: userid_ua_tracker[uid][ua_match.group(1)] += 1
                        s_match = server_pattern.search(line)
                        s_val = s_match.group(1) if s_match else '99'
                        userid_server_tracker[uid] = server_map.get(s_val, 'gggg')
    except Exception as e:
        print(f"读取日志失败: {e}"); return

    # 处理封禁逻辑
    existing_userids = set()
    try:
        with open(args.banfile, 'r') as f:
            for line in f:
                parts = line.strip().split()
                if parts: existing_userids.add(parts[0].strip('"'))
    except FileNotFoundError: pass

    with open(args.banfile, 'a') as f:
        for userid, count in userid_counter.most_common():
            if count >= args.limit:
                top_ua = userid_ua_tracker[userid].most_common(1)[0][0] if userid_ua_tracker[userid] else "Unknown"
                server_name = userid_server_tracker.get(userid, 'gggg')
                
                upsert_user_data(args, userid, server_name, count, top_ua, current_ip)

                if userid not in existing_userids:
                    print(f"[🚩 新增封禁] User: {userid} | Count: {count} | Mode: {args.mode}")
                    print(f"✅ 封禁: {userid} (次数: {count})")
                    f.write(f'"{userid}" 1;\n')

                    if args.chatid and args.botkey:
	                    tg_text = (f"🚨 违规封禁 ({args.mode})\nUID: {userid}\nSrv: {server_name}\nCnt: {count}\nUA: {top_ua}\n服务器IP: {current_ip}")
	                    try:
	                        requests.get(f"https://api.telegram.org/bot{args.botkey}/sendMessage?chat_id={args.chatid}&text={tg_text}")
	                    except: pass
	
	                    try:
	                        subprocess.run(['nginx', '-s', 'reload'], check=True)
	                    except: print("Nginx reload failed")
                else:
                    print(f"[已更新DB] User: {userid} | Count: {count} | Mode: {args.mode}")

    print("✨ 任务完成")

if __name__ == "__main__":
    main()
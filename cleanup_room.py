# -*- coding: utf-8 -*-
"""清除指定教室的会话与上报数据"""
import sys

import pymysql

camera_id = sys.argv[1] if len(sys.argv) > 1 else "testroom"
conn = pymysql.connect(host="127.0.0.1", user="root", password="0112@",
                       database="study_room", charset="utf8mb4")
try:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM seat_sessions WHERE camera_id=%s", (camera_id,))
        print(f"已删除 {camera_id} 的 {cur.rowcount} 条会话")
        cur.execute("DELETE FROM records WHERE camera_id=%s", (camera_id,))
        print(f"已删除 {camera_id} 的 {cur.rowcount} 条上报记录")
    conn.commit()
finally:
    conn.close()

# 只改一张表，验证增量只处理变更表
import sqlite3
p = r"E:/open-source/factory-ontology-kit/codes/data/erp_demo.db"
conn = sqlite3.connect(p); cur = conn.cursor()
cur.execute("UPDATE equipment SET status='待机' WHERE equipment_id='EQ-005'")
conn.commit(); conn.close()
print("仅 equipment 变更: EQ-005 状态 运行中->待机")

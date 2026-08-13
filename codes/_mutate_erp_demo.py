# 修改 ERP 演示库：新增/修改/删除，模拟真实业务变更
import sqlite3, os
p = r"E:/open-source/factory-ontology-kit/codes/data/erp_demo.db"
conn = sqlite3.connect(p)
cur = conn.cursor()

# 1) equipment: 新增一台设备 EQ-005（运行中）
cur.execute("INSERT INTO equipment VALUES (?,?,?,?,?,?,?)",
            ("EQ-005", "五轴加工中心", "机加设备", "运行中", "金工车间", 75.0, "2024-05-20"))
# 2) products: 修改 P-003 价格 320 -> 350
cur.execute("UPDATE products SET price = 350.0 WHERE product_id = 'P-003'")
# 3) production_order: 删除 SO-1002，新增 SO-1004
cur.execute("DELETE FROM production_order WHERE order_id = 'SO-1002'")
cur.execute("INSERT INTO production_order VALUES (?,?,?,?,?,?,?)",
            ("SO-1004", "P-004", "EQ-005", 300, "2026-03-02", "2026-04-15", 0.0))
# 4) customer: 新增 C-04
cur.execute("INSERT INTO customer VALUES (?,?,?,?,?)",
            ("C-04", "西南精密制造厂", "西南", "13800000004", "普通"))

conn.commit()
# 打印变更后的行数
for t in ["equipment", "products", "production_order", "customer"]:
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    print(f"{t}: {cur.fetchone()[0]} 行")
conn.close()
print("DB 已修改 ✅")

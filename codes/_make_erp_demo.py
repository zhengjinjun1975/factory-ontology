import sqlite3, os
p = r"E:/open-source/factory-ontology-kit/codes/data/erp_demo.db"
if os.path.exists(p): os.remove(p)
conn = sqlite3.connect(p)
cur = conn.cursor()
cur.execute("CREATE TABLE equipment (equipment_id TEXT, equipment_name TEXT, type TEXT, status TEXT, factory_area TEXT, power_kw REAL, purchase_date TEXT)")
cur.execute("CREATE TABLE products (product_id TEXT, product_name TEXT, category TEXT, spec TEXT, unit TEXT, price REAL)")
cur.execute("CREATE TABLE production_order (order_id TEXT, product_id TEXT, equipment_id TEXT, quantity INTEGER, start_date TEXT, due_date TEXT, progress_pct REAL)")
cur.execute("CREATE TABLE customer (customer_id TEXT, customer_name TEXT, region TEXT, contact_phone TEXT, level TEXT)")
cur.executemany("INSERT INTO equipment VALUES (?,?,?,?,?,?,?)", [
  ("EQ-001","数控加工中心","机加设备","运行中","金工车间",22.0,"2021-03-15"),
  ("EQ-002","注塑机","注塑设备","待机","注塑车间",45.0,"2022-06-01"),
  ("EQ-003","装配机器人","装配设备","运行中","总装车间",8.5,"2023-01-10"),
  ("EQ-004","激光切割机","机加设备","保养","金工车间",60.0,"2020-11-20"),
])
cur.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", [
  ("P-001","精密轴承","轴承类","6204","套",18.5),
  ("P-002","注塑外壳","塑胶件","A型","件",6.8),
  ("P-003","伺服电机","电机类","750W","台",320.0),
  ("P-004","机器人关节","总成件","RJ-20","套",860.0),
])
cur.executemany("INSERT INTO production_order VALUES (?,?,?,?,?,?,?)", [
  ("SO-1001","P-001","EQ-001",5000,"2026-01-05","2026-02-20",60.0),
  ("SO-1002","P-003","EQ-004",800,"2026-01-08","2026-02-10",35.0),
  ("SO-1003","P-002","EQ-002",12000,"2026-01-12","2026-03-01",10.0),
])
cur.executemany("INSERT INTO customer VALUES (?,?,?,?,?)", [
  ("C-01","华东精工有限公司","华东","13800000001","VIP"),
  ("C-02","北方重工集团","华北","13800000002","重点"),
  ("C-03","华南汽车零部件厂","华南","13800000003","普通"),
])
conn.commit(); conn.close()
print("test db created", os.path.getsize(p), "bytes")

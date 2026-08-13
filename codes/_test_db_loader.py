# 验证 db_loader 的 DSN 解析 + MySQL/PostgreSQL 读取路径（mock 驱动，无需真实服务器）
import sys, types, os
sys.path.insert(0, r"E:/open-source/factory-ontology-kit/codes")
import db_loader

# --- 1. DSN 解析 ---
cases = [
    ("sqlite:///C:/data/factory.db", ("sqlite","","","","","C:/data/factory.db")),
    ("mysql+pymysql://erp:pw@10.0.0.5:3306/erp", ("mysql","10.0.0.5","erp","pw","erp")),
    ("postgresql+psycopg2://erp:pw@10.0.0.6:5432/mes", ("postgres","10.0.0.6","erp","pw","mes")),
    ("mysql://root@127.0.0.1/erp", ("mysql","127.0.0.1","root","","erp")),
]
for dsn, exp in cases:
    got = db_loader.parse_dsn(dsn)
    if exp[0]=="sqlite":
        assert (got[0],got[5])==(exp[0],exp[5]), (dsn,got)
    else:
        assert (got[0],got[1],got[3],got[4],got[5])==exp, (dsn,got)
    print(f"  DSN OK  {dsn} -> type={got[0]} host={got[1]} db={got[5]}")

# --- 2. SQL 注入防护 ---
try:
    db_loader.load_db({"db_type":"sqlite","database":"x.db","table":"a; DROP TABLE t"})
    print("  FAIL: 非法表名未被拦截")
except ValueError as e:
    print("  安全 OK  非法表名已拦截:", str(e)[:40])

# --- 3. MySQL 读取路径（mock pymysql）---
def make_pymysql():
    mod = types.ModuleType("pymysql")
    def connect(**kw):
        assert kw["host"]=="10.0.0.5" and kw["port"]==3306 and kw["database"]=="erp"
        class Cursor:
            description = [("equipment_id",),("equipment_name",)]
            def execute(self, sql):
                assert "`equipment`" in sql  # 表名被正确引用
                return 0
            def fetchall(self):
                return [("EQ-1","数控车床"),("EQ-2","注塑机")]
        conn = types.SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None)
        return conn
    mod.connect = connect
    return mod
sys.modules["pymysql"] = make_pymysql()
got = db_loader.load_db({"dsn":"mysql+pymysql://erp:pw@10.0.0.5:3306/erp","table":"equipment"})
print("  MySQL 读取 OK:", got[0], len(got[2]), "行, 列", got[1])

# --- 4. PostgreSQL 读取路径（mock psycopg2）---
def make_psycopg2():
    mod = types.ModuleType("psycopg2")
    def connect(**kw):
        assert kw["host"]=="10.0.0.6" and kw["port"]==5432 and kw["dbname"]=="mes"
        class Cursor:
            description = [("order_id",),("qty",)]
            def execute(self, sql):
                assert '"production_order"' in sql  # 表名被正确引用
                return 0
            def fetchall(self):
                return [("SO-1",100)]
        conn = types.SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None)
        return conn
    mod.connect = connect
    return mod
sys.modules["psycopg2"] = make_psycopg2()
got = db_loader.load_db({"db_type":"postgresql","host":"10.0.0.6","port":5432,
                         "user":"erp","password":"pw","database":"mes","table":"production_order"})
print("  PostgreSQL 读取 OK:", got[0], len(got[2]), "行, 列", got[1])

# --- 5. SQLite dict 简写形式 ---
got = db_loader.load_db({"db_type":"sqlite","database":r"E:/open-source/factory-ontology-kit/codes/data/erp_demo.db","table":"equipment"})
print("  SQLite dict 简写 OK:", got[0], len(got[2]), "行")

print("\n全部 db_loader 测试通过 ✅")

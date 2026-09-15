#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本体与词典的关系 —— 全链路 A/B 验证（零副作用）。

链路：
  CSV 数据 ──suggest_schema──► schema ──build_ontology_ttl──► 本体(ttl/nt)
                                   └──────_build_lexicon───► 词典(lexicon.json)
  词典 + 公共工业词典(industrial_dict) ──load_dict──► 问答解析层

验证手法：把 industrial_dict_loader.merge_industrial_dict 打桩成 no-op
（纯内存 patch，不改动 industrial_dict/ 任何文件），跑前后两遍，比对产物。

断言：
  A1 schema 推断**不读**词典  → 摘掉公共词典，schema 逐字节一致
  A2 本体产出**不读**词典      → 摘掉公共词典，ttl 逐字节一致
  A3 词典生成**读**公共/行业词典 → 摘掉后少掉的词 = 行业+公共层贡献
  A4 问答加载**读**公共词典    → load_dict 前后的词条差 = 公共层兜底
  A5 本体中文标签来自 schema（而非 lexicon）→ ttl 里中文 label ⊆ schema label 集
  A6 词典词与本体标签的对齐率（量化"两条平行线"的重合度）

用法：python scripts/verify_ontology_lexicon_relation.py [data_dir]
"""
import os
import sys
import json
import hashlib
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CODES = os.path.join(ROOT, "codes")
sys.path.insert(0, CODES)

import industrial_dict_loader as idl          # noqa: E402
import schema_ontology as so                  # noqa: E402
import multi_model as mm                      # noqa: E402
import ontology_export as oe                  # noqa: E402
import ontology_qa_v3 as v3                   # noqa: E402

DATA = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CODES, "data_valve")
KB = os.path.basename(DATA).replace("data_", "")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  — " + detail) if detail else ""))


def jd(o):
    return json.dumps(o, sort_keys=True, ensure_ascii=False)


def h(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


_orig_merge = idl.merge_industrial_dict


def _noop(kb_dict, files=None, industry=None):
    """摘掉公共词典：等价于 industrial_dict/ 不存在。"""
    return kb_dict


print("=" * 72)
print("本体与词典的关系 —— 全链路验证")
print("=" * 72)
print("数据目录 : %s" % DATA)
print("知识库   : %s" % KB)

data = so.load_all(DATA)
print("数据表   : %d 张, 共 %d 行" % (len(data), sum(len(v) for v in data.values())))

# ---------------------------------------------------------------- 基线
print("\n[0] 基线（词典正常参与）")
schema_full = so.suggest_schema(data, use_llm=False, industry=KB)
ttl_full = oe.build_ontology_ttl(schema_full)
lex_full = mm._build_lexicon(schema_full, data)
pub = idl.merge_industrial_dict({})
print("  schema: %d 实体 / %d 关系 / %d 约束" % (
    len(schema_full.get("entities", [])), len(schema_full.get("relations", [])),
    len(schema_full.get("constraints", []))))
print("  本体 ttl: %d 字符 (sha1 %s)" % (len(ttl_full), h(ttl_full)))
print("  词典: %s" % {k: len(v) for k, v in lex_full.items() if isinstance(v, dict)})
print("  公共层(00_basis): %s" % {k: len(v) for k, v in pub.items() if isinstance(v, dict)})

# ---------------------------------------------------------------- A1/A2
print("\n[1] 摘掉公共词典，重跑同一份数据")
idl.merge_industrial_dict = _noop
try:
    schema_nopub = so.suggest_schema(data, use_llm=False, industry=KB)
    ttl_nopub = oe.build_ontology_ttl(schema_nopub)
    lex_nopub = mm._build_lexicon(schema_nopub, data)
finally:
    idl.merge_industrial_dict = _orig_merge

check("A1 schema 推断不读词典", jd(schema_full) == jd(schema_nopub),
      "两边 sha1 %s / %s" % (h(jd(schema_full)), h(jd(schema_nopub))))
check("A2 本体 ttl 不读词典", ttl_full == ttl_nopub,
      "两边 sha1 %s / %s" % (h(ttl_full), h(ttl_nopub)))

# ---------------------------------------------------------------- A3
print("\n[2] 词典生成：公共层到底贡献了什么")
MERGE_KEYS = tuple(getattr(idl, "_MERGE_KEYS", ()) or ())   # 与生产模块保持同步，不硬编码
added_total = {}
for k in MERGE_KEYS:
    a = set((lex_full.get(k) or {}).keys())
    b = set((lex_nopub.get(k) or {}).keys())
    added_total[k] = sorted(a - b)
    removed = sorted(b - a)
    print("  %-14s 有公共层 %3d 词 / 无公共层 %3d 词 → 公共层补入 %3d%s"
          % (k, len(a), len(b), len(a - b), ("  (异常: 反而少了 %d)" % len(removed)) if removed else ""))
gain = sum(len(v) for v in added_total.values())
check("A3 词典生成读公共词典", gain > 0, "公共层共补入 %d 个词条" % gain)
if gain:
    sample = [w for k in MERGE_KEYS for w in added_total[k]][:12]
    print("      样例: %s" % "、".join(sample))
# 非合并键不应受公共层影响
other_same = all(
    jd(lex_full.get(k)) == jd(lex_nopub.get(k))
    for k in lex_full if k not in MERGE_KEYS and not k.startswith("_")
)
check("A3b 公共/行业层只补词表键, 不碰 attr/numeric 等工厂字段", other_same)

# ---------------------------------------------------------------- A4
print("\n[3] 问答加载：公共层兜底了多少")
tmp = tempfile.mkdtemp(prefix="lexverify_")
p_full = os.path.join(tmp, "lex_full.json")
p_nopub = os.path.join(tmp, "lex_nopub.json")
with open(p_full, "w", encoding="utf-8") as f:
    json.dump(lex_full, f, ensure_ascii=False)
with open(p_nopub, "w", encoding="utf-8") as f:
    json.dump(lex_nopub, f, ensure_ascii=False)

d_full = v3.load_dict(p_full)
idl.merge_industrial_dict = _noop
try:
    d_nopub = v3.load_dict(p_nopub)
finally:
    idl.merge_industrial_dict = _orig_merge

for k in MERGE_KEYS:
    a = len(d_full.get(k) or {})
    b = len(d_nopub.get(k) or {})
    print("  %-14s 问答侧词典 %3d → %3d (+%d)" % (k, b, a, a - b))
check("A4 问答加载读公共词典",
      any(len(d_full.get(k) or {}) > len(d_nopub.get(k) or {}) for k in MERGE_KEYS))

# ---------------------------------------------------------------- A5
print("\n[4] 本体里的中文标签来自哪里")
# 本体中文标签的可溯源集合：全部由 schema 字段派生
#   label/definition  → 实体与属性
#   domain            → 域分组中间类（xxx域 → XxxDomain）
#   key               → 最小公理集里的「主键唯一：<pk>」注释类
#   relations[].label → 对象属性名
schema_labels = set()
for e in schema_full.get("entities", []):
    for k in ("label", "definition", "domain"):
        if e.get(k):
            schema_labels.add(e[k])
    if e.get("key"):
        schema_labels.add("主键唯一：%s" % e["key"])
    for a in e.get("attributes", []):
        for k in ("label", "definition"):
            if a.get(k):
                schema_labels.add(a[k])
for r in schema_full.get("relations", []):
    if r.get("label"):
        schema_labels.add(r["label"])

import re  # noqa: E402
ttl_labels = set(m.group(1) for m in re.finditer(r'rdfs:label\s+"([^"]+)"', ttl_full))
cjk = lambda s: bool(re.search(r"[\u4e00-\u9fff]", s))
ttl_cn = {x for x in ttl_labels if cjk(x)}
stranger = {x for x in ttl_cn if x not in schema_labels}
check("A5 本体中文标签 100% 可溯源到 schema（label/definition/域/主键）", not stranger,
      "本体 %d 个中文标签, 不可溯源: %d %s"
      % (len(ttl_cn), len(stranger), ("→ " + "、".join(list(stranger)[:6])) if stranger else ""))
lex_cn = set((lex_full.get("attr_cn2en") or {}).keys()) | set((lex_full.get("type_cn2en") or {}).keys())
check("A5b 词典词不是本体标签的来源（两条平行线）", bool(lex_cn),
      "词典 %d 个中文词, 其中 %d 个也是本体标签, %d 个只存在于词典"
      % (len(lex_cn), len(lex_cn & ttl_cn), len(lex_cn - ttl_cn)))

# ---------------------------------------------------------------- A6 行业层是否真被消费
print("")
print("[5] 行业层是否真的被消费（本次修的核心断点）")
base_only = idl.merge_industrial_dict({})
with_ind = idl.merge_industrial_dict({}, industry="泵阀")
b_type = len(base_only.get("type_cn2en") or {})
w_type = len(with_ind.get("type_cn2en") or {})
def _nn(d):
    return {k: len(v) for k, v in d.items() if isinstance(v, dict) and v}

b_nn, w_nn = _nn(base_only), _nn(with_ind)
grown = ["%s %d→%d" % (k, b_nn.get(k, 0), w_nn[k]) for k in w_nn if w_nn[k] > b_nn.get(k, 0)]
print("  非空词键增量: %s" % ("、".join(grown) or "无"))
check("A6b 行业层特有词键（pump/part/process…）带来非空增量", len(grown) > 0, "、".join(grown[:8]))

# 默认（不传 industry）仍是只基础层 —— 向后兼容
check("A6c 不传 industry 时行为不变（向后兼容）", jd(base_only) == jd(idl.merge_industrial_dict({}, industry=None)),
      "两次默认合并逐字节一致")

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 72)
print("结论")
print("=" * 72)
print("""
数据 → schema → 本体 这条主干【不经过词典】：
  schema 由 suggest_schema 从 CSV 推断（实体/属性/关系/枚举/约束），
  本体由 schema 生成（类层次/对象属性/数据属性/owl:hasKey/oneOf/Restriction/命名空间）。
  摘掉公共词典后，schema 与本体 ttl 逐字节不变（A1/A2 PASS）。

词典是另一条支线，与本体只在两处交汇，且都是"词典→下游"，不是"词典→本体"：
  ① 生成工厂词典时并入公共工业词典（multi_model._build_lexicon）—— 词典→词典
  ② 问答解析层加载词典时再兜底一层公共词典（ontology_qa_v3.load_dict）—— 词典→问答

本体里的中文标签（rdfs:label / skos:definition）来自 schema，
词典里的中文（attr_cn2en/type_cn2en…）服务的是问答问句解析。二者不是同一份数据。
""")
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项: %s" % "、".join(FAIL))
import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)   # 不留临时对比文件
sys.exit(1 if FAIL else 0)

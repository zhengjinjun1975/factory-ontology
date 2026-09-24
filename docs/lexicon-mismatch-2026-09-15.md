# 词典错配清单（只读扫描，2026-09-15）

扫描范围：`codes/config/lexicon_{valve,food,chem,auto_parts}.json`
扫描方式：只读，未改动任何词典文件。

## 为什么记这份清单

排查"运行中的设备有多少台 → 有 0 台"时，一路下钻发现**不是解析逻辑错，是词典被污染**。
代码层已兜住（见下），但词典里的错配还在。记在这里，免得下次再从同一个坑往下挖。

## 错配清单（15 条）

### 真错配 —— 语义错乱，建议修

| 库 | 位置 | 内容 | 问题 |
|---|---|---|---|
| valve | `synonym_map` | `'设备' ← ['运行中']` | **状态词挂到实体名下**。`_find_enum('运行中的设备有多少台','type')` 返回 `('equipment','设备')` → 抢走"状态+类型"组合分支 → 答 0 |
| valve | `synonym_map` | `'状态' ← ['已入库','待机','待质检','生产中','不合格','合格','运行中']` | 把泛称"状态"当成枚举值的规范词 |

### 待判断 —— 需业务确认，我不擅自定

| 库 | 位置 | 内容 | 说明 |
|---|---|---|---|
| valve/food/chem/auto_parts | `type_cn2en` | `'设备' → 'equipment'` | 若业务上确实存在 `type='设备'` 这个取值，该留；若只是实体名误入，该删。**无法从数据判定** |
| valve | `synonym_map` | 以实体名作规范词：`设备 / 原料 / 客户 / 产品 / 质检 / 销售` | 会让实体名被当枚举值返回（本次踩的坑）。若组内成员确为同义，可保留组、只把规范词降级为普通成员 |

### 看着像错、其实合理 —— 不动

| 库 | 位置 | 内容 | 说明 |
|---|---|---|---|
| food/chem/auto_parts | `synonym_map` | `'原料' ← ['原材料']` | 合法同义词 |

## 代码层已做的兜底（`ontology_qa_v3._find_enum`）

词典脏不该让问答错答。已在代码层加四处防护：

1. 路径1（枚举词典直接命中）：命中词若在 `entity_cn2en` 里 → 跳过
2. 路径2 第一段（同义词反查）：规范词若在 `entity_cn2en` 里 → 跳过
3. 路径2 第二段：候选词**必须出现在问句里**，且不得是实体名
4. `_field`：`aliases` 为 None 或**值为空列表**时回落默认别名

> `.get(key, default)` 对"键存在但值为空列表"**不返回 default** —— 这是本次第 2 个坑
> （valve 的 `field_aliases` 曾出现 `'status': []`）。写取值器时默认怀疑这种形态。

## 复核命令

```bash
cd <repo>/codes
python - <<'PY'
import json, os
STATUS = set("运行中 运行 正常 工作中 停止 停机 空闲 待机 故障 报警 异常 维护 保养 维修 离线 "
             "合格 不合格 已入库 待质检 生产中 待检修".split())
for kb in ['valve','food','chem','auto_parts']:
    p = f'config/lexicon_{kb}.json'
    if not os.path.exists(p): continue
    d = json.load(open(p, encoding='utf-8'))
    ents = set(d.get('entity_cn2en') or {})
    for w in ('status_cn2en','type_cn2en','zone_cn2en'):
        for k in (d.get(w) or {}):
            if k in ents: print(f'{kb} {w} 实体名混入: {k}')
    for canon, grp in (d.get('synonym_map') or {}).items():
        ms = [canon] + list(grp)
        st = [m for m in ms if m in STATUS]
        if st and canon not in STATUS: print(f'{kb} synonym 状态词并入非状态组: {canon} <- {st}')
PY
```

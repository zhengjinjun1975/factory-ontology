# -*- coding: utf-8 -*-
"""ontology_evolve.py — 本体自演进（增量：新实体候选 → 待确认 → 并入词典）

移植自 opa-monitor/modules/ontology_evolve.py，按 factory-ontology 架构适配：
  · 多 kb：所有操作**按 kb 走**（每个 kb 一份演进状态 + 一份词典）
  · 抽实体：复用本库 model_llm.llm_generate（不再自建 urllib 调 ollama）
  · 别名落点：写进 codes/config/lexicon_<kb>.json 的 synonym_map（字段/实体别名表）
  · 状态落点：codes/config/evolve_<kb>.json（待确认 / 拒绝 / 版本 / 快照索引）

三道安全设计（与 opa 版一致，勿省）：
  ① 新实体**不自动进词典** —— 只进「待确认区」，人拍板才并；
  ② 版本 + 审计（confirm/reject/rollback 都留痕）；
  ③ 可回退 —— 每次变更前存快照（**词典 + 演化节点**），rollback 一并回退（版本单调递增，历史无歧义）。
"""
import io
import json
import os
import re
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(_HERE, "config")
KBS_PATH = os.path.join(CONFIG_DIR, "kbs.json")

_PENDING = "pending"
_REJECT_KEY = "rejected"
_VERSION_KEY = "version"
_SNAPSHOT_KEY = "snapshots"

# 本体自演进用的模型（默认走本库统一定义；可被 env 覆盖）
_ENV_MODEL_KEY = "FACTORY_EVOLVE_MODEL_KEY"

# 媒体名/来源名（LLM 会当专名抽出来，但它们是来源不是实体）
_MEDIA_NAMES = {
    "it之家", "36氪", "虎嗅", "澎湃", "澎湃新闻", "新浪", "新浪科技", "财新", "界面",
    "界面新闻", "第一财经", "每日经济新闻", "每经", "证券时报", "中国证券报", "中证",
    "观察者", "观察者网", "环球", "环球时报", "参考消息", "央视", "央视新闻", "新华社",
    "人民日报", "中国日报", "科技日报", "凤凰", "凤凰网", "网易", "腾讯新闻", "ithome",
    "公众号", "微信公众号", "知乎", "b站", "bilibili", "小红书", "微博", "抖音", "快手",
    "视频号", "百家号", "头条", "今日头条", "csdn", "掘金", "简书", "豆瓣", "贴吧",
}

_PROMPT = (
    "从下面文本里抽取实体名（公司/机构/品牌/产品型号/设备名等专有名词）。\n"
    "只抽专有名词；不要通用词（设备/产品/客户/订单/状态/数量/系统…）、"
    "不要媒体名（IT之家/36氪/虎嗅/澎湃…）、不要单位与数量。宁可少抽，不要凑数。\n"
    '只输出 JSON：{"entities": ["…"]}\n\n文本：'
)


# ─────────────────────────── 路径与读写 ───────────────────────────

def _kb_names():
    try:
        with io.open(KBS_PATH, encoding="utf-8") as f:
            return list((json.load(f).get("kbs") or {}).keys())
    except Exception:
        return []


def _norm_kb(kb=None):
    """kb 归一：显式给就用；否则用 kbs.json 里第一个（无则 default）。"""
    if kb:
        return str(kb)
    names = _kb_names()
    return names[0] if names else "default"


def _lexicon_path(kb):
    """该 kb 的词典文件路径（kbs.json 的 lexicon 字段优先，回退约定命名）。"""
    try:
        with io.open(KBS_PATH, encoding="utf-8") as f:
            meta = (json.load(f).get("kbs") or {}).get(kb) or {}
        if meta.get("lexicon"):
            return os.path.join(CONFIG_DIR, meta["lexicon"])
    except Exception:
        pass
    return os.path.join(CONFIG_DIR, "lexicon_%s.json" % kb)


def _evolve_path(kb):
    return os.path.join(CONFIG_DIR, "evolve_%s.json" % kb)


def _snap_dir(kb):
    return os.path.join(CONFIG_DIR, "evolve_snapshots", kb)


def _read_json(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, doc):
    """原子写（tmp → replace）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _state(kb):
    d = _read_json(_evolve_path(kb), {})
    d.setdefault(_PENDING, [])
    d.setdefault(_REJECT_KEY, [])
    d.setdefault(_VERSION_KEY, 1)
    d.setdefault(_SNAPSHOT_KEY, [])
    return d


def _save_state(kb, d):
    _write_json(_evolve_path(kb), d)


# ─────────────────────────── 现有词典视图 ───────────────────────────

def _lexicon(kb):
    return _read_json(_lexicon_path(kb), {})


def _existing_names(kb):
    """已在词典里的名字（含 synonym_map 的键与值、entity_cn2en 的键）—— 不重复提。"""
    lex = _lexicon(kb)
    names = set()
    for k, vs in (lex.get("synonym_map") or {}).items():
        names.add(str(k))
        for v in (vs or []):
            names.add(str(v))
    for k in (lex.get("entity_cn2en") or {}):
        names.add(str(k))
    return names


# ─────────────────────────── 抽实体 ───────────────────────────

def _llm_extract_one(text):
    """抽一条文本的实体。失败返回 None（调用方回退滑窗）。"""
    try:
        from model_llm import llm_generate
    except ImportError:
        try:
            import model_llm                                    # type: ignore
            llm_generate = model_llm.llm_generate
        except Exception:
            return None
    try:
        out = llm_generate(_PROMPT + (text or "")[:1500], temperature=0.0,
                           max_tokens=300,
                           model_key=os.environ.get(_ENV_MODEL_KEY) or "local")
        if out and str(out).lstrip().startswith("[模型错误]"):
            return None
    except Exception:
        return None
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return []
    try:
        ents = json.loads(m.group(0)).get("entities") or []
    except Exception:
        return []
    got = []
    for e in ents:
        e = str(e).strip()
        if (e and 2 <= len(e) <= 24 and e.lower() not in _MEDIA_NAMES
                and e not in got):
            got.append(e)
    return got


def _grams(text, n=(2, 3, 4)):
    """滑窗提词（兜底用：LLM 不可用时仍能工作）。"""
    t = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text or "")
    out = []
    for k in n:
        for i in range(len(t) - k + 1):
            g = t[i:i + k]
            if re.search(r"[\u4e00-\u9fff]", g) or len(g) >= 2:
                out.append(g)
    return out


def _drop_fragments(cands, keep_ratio=0.6, max_len=3):
    """去碎片：滑窗会切出与更长候选重叠的短碎片。"""
    names = [c["name"] for c in cands]
    dropped = []
    for c in cands:
        x = c["name"]
        if len(x) > max_len:
            continue
        for y in names:
            if len(y) > len(x) and x in y and y != x:
                yc = next((d["count"] for d in cands if d["name"] == y), 0)
                if yc >= c["count"] * keep_ratio:
                    dropped.append(x)
                    break
    keep = [c for c in cands if c["name"] not in set(dropped)]
    return keep, dropped


# ─────────────────────────── 主流程 ───────────────────────────

def propose(texts, kb=None, min_count=2, limit=20, use_llm=None):
    """从文本提候选 → 只落「待确认区」（不污染词典）。返回候选列表。"""
    kb = _norm_kb(kb)
    have, st = _existing_names(kb), _state(kb)
    rej = set(st.get(_REJECT_KEY) or [])
    if use_llm is None:
        use_llm = os.environ.get("FACTORY_EVOLVE_LLM", "1").lower() not in ("0", "false", "no", "off")

    cands = []
    if use_llm:
        cnt, sample, ok_any = {}, {}, False
        for t_ in (texts or []):
            got = _llm_extract_one(t_)
            if got is None:
                continue
            ok_any = True
            for e in got:
                if e in have or e in rej:
                    continue
                cnt[e] = cnt.get(e, 0) + 1
                sample.setdefault(e, (t_ or "")[:60])
        if ok_any:
            cands = [{"name": n, "count": c, "sample": sample.get(n, "")}
                     for n, c in sorted(cnt.items(), key=lambda kv: -kv[1])
                     if c >= min_count][:limit]

    if not cands:
        from collections import Counter
        cnt, sample = Counter(), {}
        for t_ in (texts or []):
            for g in _grams(t_):
                if g in have or g in rej:
                    continue
                cnt[g] += 1
                sample.setdefault(g, (t_ or "")[:60])
        cands = [{"name": n, "count": c, "sample": sample.get(n, "")}
                 for n, c in cnt.most_common(limit) if c >= min_count]
        cands, _ = _drop_fragments(cands)

    if cands:
        cur = st.get(_PENDING) or []
        seen = {c["name"] for c in cur}
        cur.extend(c for c in cands if c["name"] not in seen)
        st[_PENDING] = cur
        _save_state(kb, st)
    return cands


def pending(kb=None):
    return _state(_norm_kb(kb)).get(_PENDING) or []


def alias_version(kb=None):
    return int(_state(_norm_kb(kb)).get(_VERSION_KEY) or 1)


def _snap(kb, version, lex):
    """存快照（版本号 = 该快照对应的版本）：**词典 + 演化节点**。

    节点必须一起存 —— 否则 rollback 只撤词典、在图上留下孤儿节点（回退不对称）。
    """
    d = _snap_dir(kb)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "lexicon.v%d.json" % version)
    _write_json(p, lex)
    _write_json(os.path.join(d, "nodes.v%d.json" % version), load_evolve_nodes(kb))
    return p


def _restore_nodes(kb, version):
    """从快照恢复演化节点，与词典回退对齐。

    返回恢复后的节点数；无该版本的节点快照（老快照）→ None（不动现状，不抛错）。
    恢复为空集时删掉节点文件，不留空壳。
    """
    p = os.path.join(_snap_dir(kb), "nodes.v%d.json" % version)
    doc = _read_json(p, None)
    if not isinstance(doc, dict):
        return None
    nodes = doc.get("nodes") or []
    if not nodes:
        try:
            os.remove(_evolve_nodes_path(kb))
        except Exception:
            pass
        return 0
    _save_evolve_nodes(kb, doc)
    return len(nodes)


def snapshots(kb=None):
    kb = _norm_kb(kb)
    d = _snap_dir(kb)
    if not os.path.isdir(d):
        return []
    vs = []
    for fn in os.listdir(d):
        m = re.fullmatch(r"lexicon\.v(\d+)\.json", fn)
        if m:
            vs.append(int(m.group(1)))
    return sorted(vs)


def confirm(name, node=None, kb=None, aliases=None, actor="human"):
    """确认候选：并入词典 synonym_map + 版本+1 + 审计 + 快照。返回 (ok, 说明)。"""
    kb = _norm_kb(kb)
    if not name:
        return False, "候选名不能为空"
    lex = _lexicon(kb)
    if not lex:
        return False, "词典不存在或不可读: %s" % _lexicon_path(kb)
    sm = lex.setdefault("synonym_map", {})
    key = node or name
    exist = sm.get(key) or []
    if name in exist or key == name and exist:
        return False, "%s 已在词典里了" % name

    st = _state(kb)
    ver = int(st.get(_VERSION_KEY) or 1)
    _snap(kb, ver, lex)                                  # 变更前存快照
    sm[key] = sorted(set(list(exist) + [name] + list(aliases or [])))
    lex["_evolve_note"] = "本体自演进并入（%s，actor=%s，版本→%d）" % (
        name, actor, ver + 1)
    _write_json(_lexicon_path(kb), lex)

    st[_VERSION_KEY] = ver + 1
    st[_SNAPSHOT_KEY] = sorted(set(list(st.get(_SNAPSHOT_KEY) or []) + [ver]))
    st[_PENDING] = [c for c in (st.get(_PENDING) or []) if c.get("name") != name]
    _save_state(kb, st)
    _audit(kb, "ontology_evolve_confirm", key,
           {"name": name, "version": ver + 1, "actor_kind": actor})
    return True, "已并入 %s，词典版本 → %d" % (key, ver + 1)


def reject(name, kb=None, actor="human"):
    """拒绝候选：记进拒绝区（不再提），不进词典。"""
    kb = _norm_kb(kb)
    if not name:
        return False, "候选名不能为空"
    st = _state(kb)
    rej = set(st.get(_REJECT_KEY) or [])
    if name in rej:
        return False, "%s 已被拒过" % name
    rej.add(name)
    st[_REJECT_KEY] = sorted(rej)
    st[_PENDING] = [c for c in (st.get(_PENDING) or []) if c.get("name") != name]
    _save_state(kb, st)
    _audit(kb, "ontology_evolve_reject", name, {"actor_kind": actor})
    return True, "已拒绝 %s（不再提）" % name


def rollback(target_version=None, kb=None, actor="human"):
    """回退词典到某版本（默认上一版）。回退本身是一次变更 ⇒ 版本继续 +1。"""
    kb = _norm_kb(kb)
    st = _state(kb)
    cur = int(st.get(_VERSION_KEY) or 1)
    vs = snapshots(kb)
    if not vs:
        return False, "没有可用快照（快照在第一次 confirm 时开始产生）"
    if target_version is None:
        lower = [v for v in vs if v < cur]
        if not lower:
            return False, "已是可回退的最早版本(v%d)" % cur
        target_version = lower[-1]
    if target_version not in vs:
        return False, "没有 v%d 的快照（可用: %s）" % (target_version, vs)

    _snap(kb, cur, _lexicon(kb))                         # 回退前先把当前版存一份
    src = os.path.join(_snap_dir(kb), "lexicon.v%d.json" % target_version)
    doc = _read_json(src, None)
    if doc is None:
        return False, "快照 v%d 读不出" % target_version
    doc["rolled_back_from"] = cur
    doc["rolled_back_to"] = target_version
    _write_json(_lexicon_path(kb), doc)
    # 节点与词典**对称**回退（修缺口1）: 否则回退后词典里没了、图上还留着孤儿节点。
    n_restored = _restore_nodes(kb, target_version)

    st[_VERSION_KEY] = cur + 1
    st[_SNAPSHOT_KEY] = sorted(set(list(st.get(_SNAPSHOT_KEY) or []) + [cur]))
    _save_state(kb, st)
    _audit(kb, "ontology_evolve_rollback", str(target_version),
           {"from_version": cur, "to_version": target_version, "new_version": cur + 1,
            "nodes_restored": n_restored})
    if n_restored is None:
        return True, "已回退到 v%d 的内容，词典版本 → %d（该版本无节点快照，演化节点未动）" % (
            target_version, cur + 1)
    return True, "已回退到 v%d 的内容，词典版本 → %d，演化节点同步回退为 %d 个" % (
        target_version, cur + 1, n_restored)


def history(kb=None):
    kb = _norm_kb(kb)
    st = _state(kb)
    return {"kb": kb, "version": st.get(_VERSION_KEY), "snapshots": snapshots(kb),
            "pending": len(st.get(_PENDING) or []), "rejected": len(st.get(_REJECT_KEY) or [])}


def _audit(kb, action, target, detail):
    """写审计（复用本库 audit_chain；不可用时不拖垮演进本身）。"""
    try:
        import api_server  # noqa: F401  (确保 app context)
    except Exception:
        pass
    try:
        import audit_chain
        ac = None
        for attr in ("AuditChain",):
            cls = getattr(audit_chain, attr, None)
            if cls is not None:
                try:
                    ac = cls()
                    break
                except Exception:
                    ac = cls
        if ac is not None and hasattr(ac, "append"):
            ac.append({"action": action, "target": target, "detail": detail, "kb": kb})
            return
    except Exception:
        pass
    # 回退：落一份轻量审计（保证留痕不断链）
    try:
        p = os.path.join(CONFIG_DIR, "evolve_audit.jsonl")
        with io.open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kb": kb,
                                "action": action, "target": target, "detail": detail},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


import io
import json
import os
import datetime as _dt


def _evolve_nodes_path(kb):
    """演化节点文件路径：config/evolve_nodes_<kb>.json"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'evolve_nodes_%s.json' % kb)


def load_evolve_nodes(kb):
    """读演化节点。文件不存在/损坏 → fail-open 返回空结构，不抛异常。"""
    try:
        with io.open(_evolve_nodes_path(kb), encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError('not a dict')
        d.setdefault('kb', kb)
        d.setdefault('version', 0)
        d.setdefault('nodes', [])
        if not isinstance(d['nodes'], list):
            d['nodes'] = []
        return d
    except Exception:
        return {'kb': kb, 'version': 0, 'nodes': []}


def _save_evolve_nodes(kb, data):
    """原子写（tmp + os.replace）。"""
    p = _evolve_nodes_path(kb)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
    except Exception:
        pass
    tmp = p + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def add_evolve_node(name, cls, kb, aliases=None):
    """往演化节点列表追加一个节点（同名已存在则不动）。返回 (ok, msg)。"""
    if not name:
        return (False, 'name 为空')
    d = load_evolve_nodes(kb)
    for n in d.get('nodes') or []:
        if n.get('name') == name:
            return (False, '节点已存在: %s' % name)
    uri = '%s_%s' % (cls or 'Evolved', name)
    node = {'uri': uri, 'cls': cls or 'Evolved', 'name': name,
            'aliases': list(aliases or []),
            'created_at': _dt.datetime.now().isoformat(timespec='seconds')}
    d.setdefault('nodes', []).append(node)
    d['version'] = int(d.get('version') or 0) + 1
    _save_evolve_nodes(kb, d)
    return (True, '已新增节点 %s' % uri)


def remove_evolve_node(name, kb):
    """按 name 删除演化节点（供 reject / 回退用）。返回 (ok, msg)。"""
    d = load_evolve_nodes(kb)
    before = len(d.get('nodes') or [])
    d['nodes'] = [n for n in (d.get('nodes') or []) if n.get('name') != name]
    if len(d['nodes']) == before:
        return (False, '未找到节点: %s' % name)
    d['version'] = int(d.get('version') or 0) + 1
    _save_evolve_nodes(kb, d)
    return (True, '已移除节点 %s' % name)

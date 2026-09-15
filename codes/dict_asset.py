"""工厂词典资产导出/导入模块。

用途: 支撑"企业A 结束导出 → 同行业企业B 导入复用 → 继续累积"的数据资产闭环。
CLI:
    python dict_asset.py export <kb> [out_dir]
    python dict_asset.py import <kb> <src_path> [--replace] [--dry-run]
    python dict_asset.py bundle <kb> [out_dir]
    python dict_asset.py restore <zip_path> [--dry-run]

定位: 零依赖、只做词典资产进出口, 不参与建模/问答。
"""

import os
import sys
import json
import shutil
import zipfile
import hashlib
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(ROOT, "config")
OUTPUT_DIR = os.path.join(ROOT, "output")
MERGE_KEYS = ("type_cn2en", "status_cn2en", "synonym_map", "entity_cn2en", "fault_cn2en",
              "material_synonyms", "pump_cn2en", "part_cn2en", "process_cn2en",
              "product_type_cn2en", "safety_cn2en", "method_cn2en")


def _load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def verify_lexicon(d):
    """校验词典结构, 返回问题列表(空 = 通过)。"""
    if not isinstance(d, dict):
        return ["词典必须是 JSON 对象"]
    problems = []
    present = 0
    for key in MERGE_KEYS:
        if key not in d:
            continue
        present += 1
        val = d[key]
        if not isinstance(val, dict):
            problems.append("%s 值必须是对象" % key)
            continue
        for k, v in val.items():
            if not isinstance(k, str):
                problems.append("%s 键必须是字符串: %s" % (key, k))
            # 值允许字符串，或字符串列表（synonym_map 的值是「同义词组」list，实测全库 1197 条皆 list）
            elif not (isinstance(v, str)
                      or (isinstance(v, list) and all(isinstance(x, str) for x in v))):
                problems.append("%s 值必须是字符串或字符串列表: %s" % (key, k))
    if present == 0:
        problems.append("词典不含任何有效词表键")
    return problems


def lexicon_diff(base, other):
    """返回 base 与 other 的差异报告。"""
    base = base if isinstance(base, dict) else {}
    other = other if isinstance(other, dict) else {}
    added = {}
    changed = {}
    base_total = 0
    other_total = 0
    added_total = 0
    for key in MERGE_KEYS:
        b = base.get(key) if isinstance(base.get(key), dict) else {}
        o = other.get(key) if isinstance(other.get(key), dict) else {}
        base_total += len(b)
        other_total += len(o)
        new_words = sorted(w for w in o if w not in b)
        if new_words:
            added[key] = new_words[:200]
            added_total += len(new_words)
        chg = []
        for w in o:
            if w in b and b[w] != o[w]:
                chg.append([w, b[w], o[w]])
        if chg:
            changed[key] = chg
    return {
        "added": added,
        "changed": changed,
        "stats": {
            "base_total": base_total,
            "other_total": other_total,
            "added_total": added_total,
        },
    }


def _lexicon_path(kb):
    return os.path.join(CONFIG_DIR, "lexicon_%s.json" % kb)


def _default_out_dir():
    return os.path.abspath(os.path.join(ROOT, "..", "dict_export"))


def export_lexicon(kb, out_dir=None):
    """导出词典到 out_dir。"""
    src = _lexicon_path(kb)
    if not os.path.exists(src):
        return {"ok": False, "error": "源词典不存在: %s" % src}
    data = _load_json(src, {})
    out_dir = out_dir or _default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "lexicon_%s.json" % kb)
    shutil.copy2(src, dst)
    stats = {}
    for key in MERGE_KEYS:
        v = data.get(key)
        if isinstance(v, dict):
            stats[key] = len(v)
    return {"ok": True, "path": os.path.abspath(dst), "stats": stats}


def import_lexicon(kb, src_path, mode="merge", dry_run=False):
    """导入词典到 CONFIG_DIR/lexicon_<kb>.json。"""
    incoming = _load_json(src_path, None)
    if incoming is None:
        return {"ok": False, "error": "源词典不存在或无法解析: %s" % src_path}
    problems = verify_lexicon(incoming)
    if problems:
        return {"ok": False, "error": "校验失败: " + "; ".join(problems)}

    target = _lexicon_path(kb)
    base = _load_json(target, {})
    if not isinstance(base, dict):
        base = {}

    diff = lexicon_diff(base, incoming)

    if mode == "replace":
        merged = dict(base)
        for key in MERGE_KEYS:
            if key in incoming and isinstance(incoming[key], dict):
                merged[key] = dict(incoming[key])
    else:
        merged = dict(base)
        for key in MERGE_KEYS:
            if key not in incoming or not isinstance(incoming[key], dict):
                continue
            cur = merged.get(key)
            if not isinstance(cur, dict):
                cur = {}
            new = dict(cur)
            for w, v in incoming[key].items():
                if w not in new:
                    new[w] = v
            merged[key] = new

    stats = {}
    for key in MERGE_KEYS:
        v = merged.get(key)
        if isinstance(v, dict):
            stats[key] = len(v)

    backup = None
    if not dry_run:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.exists(target):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = "%s.bak_%s" % (target, stamp)
            shutil.copy2(target, backup)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

    return {
        "ok": True,
        "mode": mode,
        "dry_run": dry_run,
        "diff": diff,
        "backup": backup,
        "target": os.path.abspath(target),
        "stats": stats,
    }


def build_bundle(kb, out_dir=None):
    """打包词典+本体+schema 为 zip。"""
    lex = _lexicon_path(kb)
    if not os.path.exists(lex):
        return {"ok": False, "error": "源词典不存在: %s" % lex}
    data = _load_json(lex, {})
    stats = {}
    for key in MERGE_KEYS:
        v = data.get(key)
        if isinstance(v, dict):
            stats[key] = len(v)

    out_dir = out_dir or _default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(out_dir, "%s_bundle_%s.zip" % (kb, stamp))

    members = [("lexicon_%s.json" % kb, lex)]
    nt = os.path.join(OUTPUT_DIR, "%s.nt" % kb)
    if os.path.exists(nt):
        members.append(("%s.nt" % kb, nt))
    schema = os.path.join(CONFIG_DIR, "schema_%s.json" % kb)
    if not os.path.exists(schema):
        schema = os.path.join(OUTPUT_DIR, "schema_%s.json" % kb)
    if os.path.exists(schema):
        members.append(("schema_%s.json" % kb, schema))

    files = [name for name, _ in members] + ["meta.json"]   # 列出 zip 内全部成员（含 meta）
    meta = {
        "kb": kb,
        "built": datetime.now().isoformat(),
        "version": "1.0",
        "files": files,
        "stats": stats,
    }

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in members:
            zf.write(path, name)
        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

    return {
        "ok": True,
        "path": os.path.abspath(zip_path),
        "files": files,
        "stats": stats,
    }


def restore_bundle(zip_path, kb=None, dry_run=False):
    """从 build_bundle 产物恢复。"""
    if not os.path.exists(zip_path):
        return {"ok": False, "error": "zip 不存在: %s" % zip_path}

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        lex_name = None
        for n in names:
            base = os.path.basename(n)
            if base.startswith("lexicon_") and base.endswith(".json"):
                lex_name = n
                break
        if lex_name is None:
            return {"ok": False, "error": "zip 内不含 lexicon_*.json"}

        if kb is None:
            base = os.path.basename(lex_name)
            kb = base[len("lexicon_"):-len(".json")]

        tmpdir = tempfile.mkdtemp(prefix="dict_restore_")
        try:
            lex_tmp = os.path.join(tmpdir, os.path.basename(lex_name))
            with open(lex_tmp, "wb") as f:
                f.write(zf.read(lex_name))

            imported = import_lexicon(kb, lex_tmp, mode="merge", dry_run=dry_run)

            restored = []
            if not dry_run:
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                for n in names:
                    base = os.path.basename(n)
                    if base.endswith(".nt") or (base.startswith("schema_") and base.endswith(".json")):
                        dst = os.path.join(OUTPUT_DIR, base)
                        with open(dst, "wb") as f:
                            f.write(zf.read(n))
                        restored.append(base)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return {
        "ok": True,
        "kb": kb,
        "imported": imported,
        "restored": restored,
        "dry_run": dry_run,
    }


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    action = args[0]
    rest = args[1:]

    if action == "export":
        if not rest:
            print(__doc__)
            return
        kb = rest[0]
        out_dir = rest[1] if len(rest) > 1 else None
        result = export_lexicon(kb, out_dir)
    elif action == "import":
        if len(rest) < 2:
            print(__doc__)
            return
        kb, src = rest[0], rest[1]
        mode = "replace" if "--replace" in rest else "merge"
        dry = "--dry-run" in rest
        result = import_lexicon(kb, src, mode=mode, dry_run=dry)
    elif action == "bundle":
        if not rest:
            print(__doc__)
            return
        kb = rest[0]
        out_dir = rest[1] if len(rest) > 1 else None
        result = build_bundle(kb, out_dir)
    elif action == "restore":
        if not rest:
            print(__doc__)
            return
        zip_path = rest[0]
        dry = "--dry-run" in rest
        result = restore_bundle(zip_path, dry_run=dry)
    else:
        print(__doc__)
        return

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
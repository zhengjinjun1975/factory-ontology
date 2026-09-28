#!/usr/bin/env python3
"""本体自演进(ontology_evolve)端到端自检门。

真实改写运行态文件, 因此备份 + finally 逐字节还原。
用法: python scripts/verify_ontology_evolve.py
"""
import os
import sys
import json
import time
import shutil
import hashlib
import tempfile
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CODES = os.path.join(REPO, 'codes')
CONFIG = os.path.join(CODES, 'config')

PORT = int(os.environ.get('VERIFY_OE_PORT', '8941'))
READ_KEY = 'oe-read-key'
ADMIN_KEY = 'oe-admin-key'

FAILS = []
PASSES = [0]

KB = None
BASE_NODES = None


def ck(name, cond, detail=''):
    if cond:
        PASSES[0] += 1
        print('  PASS %s %s' % (name, detail))
    else:
        FAILS.append(name)
        print('  FAIL %s %s' % (name, detail))


def section(t):
    print('\n== %s ==' % t)


def http(method, path, headers=None, body=None, port=PORT, timeout=60):
    url = 'http://127.0.0.1:%d%s' % (port, path)
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            try:
                return (resp.status, json.loads(raw))
            except Exception:
                return (resp.status, None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            return (e.code, json.loads(raw))
        except Exception:
            return (e.code, None)
    except Exception:
        return (0, None)


def start_server(script, port, extra_env):
    env = dict(os.environ)
    env.pop('FOOD_STRICT_AUTH', None)
    env.update(extra_env)
    log = tempfile.NamedTemporaryFile(prefix='oe_srv_', suffix='.log', delete=False)
    proc = subprocess.Popen(
        [sys.executable, script, '--host', '127.0.0.1', '--port', str(port)],
        cwd=CODES, env=env,
        stdout=log, stderr=subprocess.STDOUT,
    )
    return proc, log.name


def wait_ready(proc, port, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        st, _ = http('GET', '/health', port=port, timeout=5)
        if st == 200:
            return True
        time.sleep(1)
    return False


def md5(path):
    if not os.path.exists(path):
        return None
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    section('启动服务')
    proc, logpath = start_server(
        'api_server.py', PORT,
        {'FOOD_ADMIN_KEY': ADMIN_KEY, 'FOOD_READ_KEY': READ_KEY,
         'FACTORY_EVOLVE_LLM': '0'},
    )
    ready = wait_ready(proc, PORT)
    ck('服务就绪', ready, 'port=%d log=%s' % (PORT, logpath))
    if not ready:
        print('服务未就绪, 中止')
        try:
            proc.terminate()
        except Exception:
            pass
        return 1

    H = {'X-API-Key': READ_KEY}
    HP = {'X-API-Key': READ_KEY, 'Content-Type': 'application/json'}

    # 确定 kb
    st, hist = http('GET', '/api/ontology/evolve/history', headers=H)
    kb = None
    if isinstance(hist, dict):
        kb = hist.get('kb')
    if not kb:
        kb = 'default'
    global KB
    KB = kb
    print('  kb=%s' % kb)

    # 备份目标
    lex = os.path.join(CONFIG, 'lexicon_%s.json' % kb)
    evo = os.path.join(CONFIG, 'evolve_%s.json' % kb)
    audit = os.path.join(CONFIG, 'evolve_audit.jsonl')
    nodes = os.path.join(CONFIG, 'evolve_nodes_%s.json' % kb)
    snaps = os.path.join(CONFIG, 'evolve_snapshots', kb)

    targets = [lex, evo, audit, nodes, snaps]
    bakdir = tempfile.mkdtemp(prefix='oe_bak_')
    existed = {}
    for t in targets:
        existed[t] = os.path.exists(t)
        if existed[t]:
            dst = os.path.join(bakdir, os.path.basename(t))
            if os.path.isdir(t):
                shutil.copytree(t, dst)
            else:
                shutil.copy2(t, dst)

    try:
        # 2) 词典不被自动污染
        section('自动提候选只进待确认区，词典逐字节不变')
        m0 = md5(lex)
        b0 = os.path.getsize(lex) if os.path.exists(lex) else -1
        st, trig = http('POST', '/api/ontology/evolve/trigger', headers=HP, body={}, timeout=180)
        m1 = md5(lex)
        b1 = os.path.getsize(lex) if os.path.exists(lex) else -1
        # 必须证明“提候选真的跑过” —— 否则触发无应答时上面的 md5 不变等于什么都没验（假绿）。
        # 所以这里注入 FACTORY_EVOLVE_LLM=0 走确定性降级提取，并断言 ok==true。
        trig_ok = isinstance(trig, dict) and trig.get('ok') is True
        cands = trig.get('candidates') if isinstance(trig, dict) else None
        ck('自动提候选真的执行了(ok==true)', trig_ok,
           'status=%s ok=%s candidates=%s' % (st, trig_ok, cands))
        ck('词典逐字节不变', m0 == m1 and b0 == b1,
           'md5_before=%s md5_after=%s bytes_before=%s bytes_after=%s' % (m0, m1, b0, b1))
        st, h_pend = http('GET', '/api/ontology/evolve/history', headers=H)
        pend = h_pend.get('pending') if isinstance(h_pend, dict) else None
        ck('待确认区可读(而不是报错)', isinstance(pend, int),
           'pending=%s status=%s' % (pend, st))
        st, _ = http('GET', '/health', headers=H)
        ck('/health 仍 200', st == 200, 'status=%s' % st)

        # 取 base 节点数 (confirm 前)
        st, g0 = http('GET', '/api/ontology/graph?kb=%s' % kb, headers=H)
        base = None
        if isinstance(g0, dict):
            ns = g0.get('nodes')
            if isinstance(ns, list):
                base = len(ns)
        global BASE_NODES
        BASE_NODES = base
        print('  base_nodes=%s' % base)

        # 真类
        st, struct = http('GET', '/api/ontology/structure', headers=H)
        cls = 'Evolved'
        if isinstance(struct, dict):
            classes = struct.get('classes')
            if isinstance(classes, list) and classes:
                cls = classes[0]
        print('  cls=%s' % cls)

        # confirm 前版本
        st, h0 = http('GET', '/api/ontology/evolve/history', headers=H)
        ver0 = None
        if isinstance(h0, dict):
            ver0 = h0.get('version')

        # 3) 人拍板
        section('人拍板 confirm')
        st, conf = http('POST', '/api/ontology/evolve/confirm', headers=HP,
                        body={'name': '__oe_gate_check__', 'cls': cls})
        ok = isinstance(conf, dict) and conf.get('ok') is True
        ver1 = conf.get('version') if isinstance(conf, dict) else None
        ck('confirm ok==true', ok, 'status=%s ok=%s' % (st, ok))
        ck('version +1', ver0 is not None and ver1 == ver0 + 1,
           'version_before=%s version_after=%s' % (ver0, ver1))

        # 4) 词典已并入
        section('词典已并入')
        lexdata = {}
        if os.path.exists(lex):
            try:
                with open(lex, encoding='utf-8') as f:
                    lexdata = json.load(f)
            except Exception:
                lexdata = {}
        sm = lexdata.get('synonym_map') if isinstance(lexdata, dict) else None
        note = lexdata.get('_evolve_note') if isinstance(lexdata, dict) else None
        ck('synonym_map 含 __oe_gate_check__',
           isinstance(sm, dict) and '__oe_gate_check__' in sm,
           'synonym_map_keys=%s' % (list(sm.keys())[:5] if isinstance(sm, dict) else sm))
        ck('_evolve_note 含 __oe_gate_check__',
           isinstance(note, str) and '__oe_gate_check__' in note,
           '_evolve_note=%s' % (note,))

        # 5) 节点文件出现
        section('节点文件出现')
        ndata = {}
        if os.path.exists(nodes):
            try:
                with open(nodes, encoding='utf-8') as f:
                    ndata = json.load(f)
            except Exception:
                ndata = {}
        nlist = ndata.get('nodes') if isinstance(ndata, dict) else None
        found = None
        if isinstance(nlist, list):
            for n in nlist:
                if isinstance(n, dict) and n.get('name') == '__oe_gate_check__':
                    found = n
                    break
        ck('evolve_nodes 存在且含节点', os.path.exists(nodes) and found is not None,
           'exists=%s node=%s' % (os.path.exists(nodes), found))

        # 6) 图上可见
        section('图上可见')
        st, g1 = http('GET', '/api/ontology/graph?kb=%s' % kb, headers=H)
        n1 = None
        has_id = False
        if isinstance(g1, dict):
            ns = g1.get('nodes')
            if isinstance(ns, list):
                n1 = len(ns)
                for n in ns:
                    if isinstance(n, dict) and '__oe_gate_check__' in str(n.get('id', '')):
                        has_id = True
                        break
        ck('节点数 == base+1', base is not None and n1 == base + 1,
           'base=%s now=%s' % (base, n1))
        ck('存在含 __oe_gate_check__ 的节点 id', has_id, 'has_id=%s' % has_id)

        # 7) 幂等
        section('幂等')
        audit_lines0 = 0
        if os.path.exists(audit):
            with open(audit, encoding='utf-8') as f:
                audit_lines0 = sum(1 for _ in f)
        st, conf2 = http('POST', '/api/ontology/evolve/confirm', headers=HP,
                         body={'name': '__oe_gate_check__', 'cls': cls})
        ok2 = isinstance(conf2, dict) and conf2.get('ok') is True
        ver2 = conf2.get('version') if isinstance(conf2, dict) else None
        audit_lines1 = 0
        if os.path.exists(audit):
            with open(audit, encoding='utf-8') as f:
                audit_lines1 = sum(1 for _ in f)
        ck('重复 confirm ok==false', not ok2, 'status=%s ok=%s' % (st, ok2))
        ck('版本号不变', ver2 == ver1, 'version_before=%s version_after=%s' % (ver1, ver2))
        ck('审计条数不变', audit_lines0 == audit_lines1,
           'audit_before=%s audit_after=%s' % (audit_lines0, audit_lines1))

        # 8) 回退对称
        section('回退必须与词典对称')
        st, rb = http('POST', '/api/ontology/evolve/rollback', headers=HP, body={})
        lexdata2 = {}
        if os.path.exists(lex):
            try:
                with open(lex, encoding='utf-8') as f:
                    lexdata2 = json.load(f)
            except Exception:
                lexdata2 = {}
        sm2 = lexdata2.get('synonym_map') if isinstance(lexdata2, dict) else None
        ck('(a) 词典不再含 __oe_gate_check__',
           not (isinstance(sm2, dict) and '__oe_gate_check__' in sm2),
           'synonym_map_keys=%s' % (list(sm2.keys())[:5] if isinstance(sm2, dict) else sm2))

        nodes_gone = True
        if os.path.exists(nodes):
            try:
                with open(nodes, encoding='utf-8') as f:
                    nd2 = json.load(f)
                nl2 = nd2.get('nodes') if isinstance(nd2, dict) else None
                if isinstance(nl2, list):
                    nodes_gone = not any(
                        isinstance(n, dict) and n.get('name') == '__oe_gate_check__'
                        for n in nl2)
            except Exception:
                nodes_gone = True
        ck('(b) 节点文件不含该名', nodes_gone,
           'exists=%s' % os.path.exists(nodes))

        st, g2 = http('GET', '/api/ontology/graph?kb=%s' % kb, headers=H)
        n2 = None
        has_id2 = False
        if isinstance(g2, dict):
            ns = g2.get('nodes')
            if isinstance(ns, list):
                n2 = len(ns)
                for n in ns:
                    if isinstance(n, dict) and '__oe_gate_check__' in str(n.get('id', '')):
                        has_id2 = True
                        break
        ck('(c) 节点数回到 base 且无该名',
           base is not None and n2 == base and not has_id2,
           'base=%s now=%s has_id=%s' % (base, n2, has_id2))

    finally:
        # 还原现场
        for t in targets:
            if existed[t]:
                dst = os.path.join(bakdir, os.path.basename(t))
                if os.path.isdir(t):
                    if os.path.exists(t):
                        shutil.rmtree(t)
                    shutil.copytree(dst, t)
                else:
                    shutil.copy2(dst, t)
            else:
                if os.path.isdir(t):
                    if os.path.exists(t):
                        shutil.rmtree(t)
                else:
                    if os.path.exists(t):
                        os.remove(t)
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # 现场还原校验：逐字节与备份对照。
    # 必须在删备份之前做 —— 否则只能看“文件在不在”，这条校验永远为真（假绿）。
    section('现场还原校验（逐字节对照）')
    for t in targets:
        dst = os.path.join(bakdir, os.path.basename(t))
        nm = os.path.basename(t)
        if existed[t]:
            if os.path.isdir(t):
                a = sorted((f, md5(os.path.join(t, f))) for f in os.listdir(t)) if os.path.isdir(t) else []
                b = sorted((f, md5(os.path.join(dst, f))) for f in os.listdir(dst)) if os.path.isdir(dst) else []
                ck('还原一致(目录) %s' % nm, a == b, 'files_now=%d files_bak=%d' % (len(a), len(b)))
            else:
                ma, mb = md5(t), md5(dst)
                ck('还原一致(逐字节) %s' % nm, ma == mb, 'now=%s bak=%s' % (ma, mb))
        else:
            ck('原本不存在且已清干净 %s' % nm, not os.path.exists(t),
               'exists=%s' % os.path.exists(t))
    shutil.rmtree(bakdir, ignore_errors=True)

    print('\n自检结果：PASS %d / FAIL %d' % (PASSES[0], len(FAILS)))
    if FAILS:
        print('失败项:')
        for f in FAILS:
            print('  - %s' % f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
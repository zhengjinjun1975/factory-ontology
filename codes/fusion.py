# D:/factory-ontology/codes/fusion.py
"""工厂本体问答 - 融合决策层。纯标准库。"""

_RULE_SOURCES = {'rule', 'logical'}
_GRAPH_SOURCES = {'graph'}
_TEXT_SOURCES = {'bm25', 'vector', 'hybrid'}
_DOC_SOURCES = {'doc'}
_NO_DATA_ANSWER = '无相关数据（该知识库不含该实体概念）'

# 候选来源权重(集中一处, 取代散落在 api_server 构造处的魔法数 0.9/1.0/0.5)。
# 用途: 候选入队时的 score; 只影响 doc/graph/text 之间的相对先后, rule/logical 由 _RULE_SOURCES 优先。
CAND_SCORE = {'rule': 1.0, 'logical': 1.0, 'doc': 0.9, 'graph': 1.0,
              'bm25': 0.5, 'vector': 0.5, 'hybrid': 0.5}
# doc 候选相关性下限(> 阈值才算相关)。0.0 = 只要过二元组闸门即相关(原行为)。
DOC_MIN_SCORE = 0.0
# 拒绝文案对外公开名(与 api_server 共用单一来源, 防双源漂移)。
NO_DATA_ANSWER = _NO_DATA_ANSWER


def _valid(c):
    return isinstance(c, dict) and c.get('answer')


def _ev(c):
    e = c.get('evidence')
    return e if isinstance(e, list) else []


def _dedup_evidence(evs):
    seen = set()
    out = []
    for e in evs:
        if not isinstance(e, dict):
            continue
        key = (e.get('entity'), e.get('class'), e.get('iri'), e.get('attr'),
               e.get('value'), e.get('source'), e.get('rule_id'))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _doc_relevant(c):
    """doc 候选相关性: score 需 > DOC_MIN_SCORE 且 evidence 非空。"""
    try:
        s = float(c.get('score', 0))
    except (TypeError, ValueError):
        s = 0.0
    return s > DOC_MIN_SCORE and bool(_ev(c))


def _pick_main(cands):
    """按优先级选主候选: rule/logical > doc(相关) > graph > bm25/vector/hybrid。"""
    for c in cands:
        if c.get('source') in _RULE_SOURCES and _ev(c):
            return c
    for c in cands:
        if c.get('source') in _DOC_SOURCES and _doc_relevant(c):
            return c
    for c in cands:
        if c.get('source') in _GRAPH_SOURCES:
            return c
    for c in cands:
        if c.get('source') in _TEXT_SOURCES:
            return c
    return None


def fuse(question, candidates, cross_domain=False, kb='', schema=None):
    if cross_domain:
        return {'ok': True, 'mode': 'cross_domain', 'answer': _NO_DATA_ANSWER,
                'evidence': [], 'engines': [], 'structured': None,
                'no_basis': True, 'confidence': 'none', 'kb': kb}

    cands = [c for c in (candidates or []) if _valid(c)]
    engines = []
    for c in cands:
        # 候选可用 engines 字段声明真实命中的引擎(如 hybrid → [bm25, vector]);
        # 未声明则回落到 source, 保证与 api_server 的 engines 词汇一致(P2-6)。
        declared = c.get('engines') if isinstance(c.get('engines'), list) else None
        for s in (declared or [c.get('source')]):
            if s and s not in engines:
                engines.append(s)

    if not cands:
        return {'ok': True, 'mode': 'none', 'answer': '', 'evidence': [],
                'engines': engines, 'structured': None,
                'no_basis': True, 'confidence': 'none', 'kb': kb}

    main = _pick_main(cands)
    if main is None:
        # 有 answer 但无任何可识别 source 的候选: 取第一个, 但不得 high
        main = cands[0]

    main_ev = _ev(main)
    merged = list(main_ev)
    for c in cands:
        if c is main:
            continue
        merged.extend(_ev(c))
    merged = _dedup_evidence(merged)
    # schema 透传: 有 schema 时给合并证据补 IRI(IRI 溯源); 无 schema 保持原样。
    if schema:
        try:
            import evidence_norm
            merged = evidence_norm.normalize_evidence(merged, schema)
        except Exception:
            pass

    answer = main.get('answer') or ''
    if not answer:
        return {'ok': True, 'mode': main.get('source') or 'none', 'answer': '',
                'evidence': merged, 'engines': engines,
                'structured': main.get('structured'), 'no_basis': True,
                'confidence': 'none', 'kb': kb}

    if not main_ev:
        confidence = 'low'
    elif main.get('source') in _RULE_SOURCES:
        confidence = 'high'
    else:
        confidence = 'medium'

    return {'ok': True, 'mode': main.get('source') or 'none', 'answer': answer,
            'evidence': merged, 'engines': engines,
            'structured': main.get('structured'), 'no_basis': not main_ev,
            'confidence': confidence, 'kb': kb}


if __name__ == '__main__':
    # 1) 规则 high
    r = fuse('q', [{'answer': 'A', 'source': 'rule', 'score': 0.9,
                    'evidence': [{'entity': 'E', 'attr': 'a', 'value': 'A',
                                  'source': 'rule', 'rule_id': 'r1'}],
                    'structured': {'x': 1}}])
    assert r['ok'] and r['confidence'] == 'high' and r['answer'] == 'A'
    assert r['mode'] == 'rule' and r['engines'] == ['rule'] and not r['no_basis']

    # 2) 有 answer 但无证据 → low
    r = fuse('q', [{'answer': 'B', 'source': 'graph', 'score': 0.5,
                    'evidence': [], 'structured': None}])
    # 无证据 → no_basis=True(原 graph 路径 no_basis = not g_ev 的语义, 防"看似有据实则空答")
    assert r['confidence'] == 'low' and r['answer'] == 'B' and r['no_basis']

    # 3) 空 answer → none
    r = fuse('q', [{'answer': '', 'source': 'bm25', 'score': 0.3,
                    'evidence': [{'entity': 'E', 'source': 'bm25'}],
                    'structured': None}])
    assert r['no_basis'] and r['confidence'] == 'none' and r['answer'] == ''

    # 4) cross_domain 拒答
    r = fuse('q', [{'answer': 'X', 'source': 'rule', 'score': 1.0,
                    'evidence': [{'entity': 'E', 'source': 'rule'}],
                    'structured': None}], cross_domain=True)
    assert r['no_basis'] and r['confidence'] == 'none'
    assert r['answer'] == _NO_DATA_ANSWER and r['evidence'] == []

    # 5) 空输入稳健
    r = fuse('q', None)
    assert r['no_basis'] and r['confidence'] == 'none'

    # 6) 优先级: doc(相关) > graph, 证据合并去重
    ev_g = {'entity': 'E', 'attr': 'a', 'value': 'V', 'source': 'graph'}
    r = fuse('q', [
        {'answer': 'G', 'source': 'graph', 'score': 0.9, 'evidence': [ev_g], 'structured': None},
        {'answer': 'D', 'source': 'doc', 'score': 0.4,
         'evidence': [{'entity': 'E', 'attr': 'a', 'value': 'V', 'source': 'doc'}], 'structured': None},
    ])
    assert r['answer'] == 'D' and r['mode'] == 'doc'
    assert r['engines'] == ['graph', 'doc']
    assert len(r['evidence']) == 2

    # 7) doc 不相关(score=0) → 落到 graph
    r = fuse('q', [
        {'answer': 'D', 'source': 'doc', 'score': 0.0, 'evidence': [], 'structured': None},
        {'answer': 'G', 'source': 'graph', 'score': 0.5, 'evidence': [ev_g], 'structured': None},
    ])
    assert r['answer'] == 'G' and r['mode'] == 'graph'

    print('fusion.py self-test OK')
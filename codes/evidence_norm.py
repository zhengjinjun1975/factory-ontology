# D:/factory-ontology/codes/evidence_norm.py
"""工厂本体问答 - 证据标准化层。纯标准库。"""

_SOURCES = {'rule', 'logical', 'graph', 'bm25', 'vector', 'doc', 'hybrid'}
_FIELDS = ('entity', 'class', 'iri', 'attr', 'value', 'source', 'score', 'rule_id', 'trace')


def _ns(schema):
    if not isinstance(schema, dict):
        return None
    ns = schema.get('namespace')
    return ns if isinstance(ns, str) and ns else None


def _join(ns, name):
    if not ns or not name:
        return None
    return ns.rstrip('#/') + '#' + str(name).lstrip('#/')


def find_iri(name, schema=None):
    try:
        if not name:
            return None
        if isinstance(schema, dict):
            ent = schema.get('entity')
            if isinstance(ent, dict) and ent.get('iri'):
                return ent['iri']
            ns = _ns(schema)
            if ns:
                return _join(ns, name)
        return None
    except Exception:
        return None


def iri_of_class(class_name, schema=None):
    try:
        if not class_name:
            return None
        if isinstance(schema, dict):
            classes = schema.get('classes')
            if isinstance(classes, dict):
                v = classes.get(class_name)
                if isinstance(v, str) and v:
                    return v
                if isinstance(v, dict) and v.get('iri'):
                    return v['iri']
            ns = _ns(schema)
            if ns:
                return _join(ns, class_name)
        return None
    except Exception:
        return None


def _one(raw, schema, source):
    if not isinstance(raw, dict):
        return None
    out = {k: raw.get(k) for k in _FIELDS}
    if out['source'] is None and source is not None:
        out['source'] = source
    if out['source'] not in _SOURCES:
        out['source'] = None
    try:
        s = out['score']
        out['score'] = float(s) if s is not None else None
        if out['score'] is not None and not (0.0 <= out['score'] <= 1.0):
            out['score'] = None
    except (TypeError, ValueError):
        out['score'] = None
    if out['iri'] is None:
        if out['entity']:
            out['iri'] = find_iri(out['entity'], schema)
        if out['iri'] is None and out['class']:
            out['iri'] = iri_of_class(out['class'], schema)
    return out


def normalize_evidence(raw, schema=None, source=None):
    try:
        if raw is None:
            return []
        items = raw if isinstance(raw, list) else [raw]
        out = []
        for it in items:
            r = _one(it, schema, source)
            if r is not None:
                out.append(r)
        return out
    except Exception:
        return []


if __name__ == '__main__':
    # 正常
    r = normalize_evidence({'entity': 'Pump', 'value': 1, 'score': 0.9, 'source': 'rule'})
    assert len(r) == 1 and r[0]['entity'] == 'Pump' and r[0]['score'] == 0.9
    assert r[0]['source'] == 'rule' and r[0]['iri'] is None
    # schema 补 iri
    sch = {'namespace': 'http://ex.org/f#'}
    r = normalize_evidence({'entity': 'Pump', 'class': 'Machine'}, schema=sch)
    assert r[0]['iri'] == 'http://ex.org/f#Pump'
    # source 参数兜底
    r = normalize_evidence({'entity': 'X'}, source='bm25')
    assert r[0]['source'] == 'bm25'
    # 非法 source 清空
    r = normalize_evidence({'entity': 'X', 'source': 'nope'})
    assert r[0]['source'] is None
    # score 越界/垃圾
    assert normalize_evidence({'score': 2})[0]['score'] is None
    assert normalize_evidence({'score': 'abc'})[0]['score'] is None
    # 空/垃圾
    assert normalize_evidence(None) == []
    assert normalize_evidence([]) == []
    assert normalize_evidence('garbage') == []
    assert normalize_evidence([1, 2, 'x']) == []
    # find_iri / iri_of_class
    assert find_iri('Pump', sch) == 'http://ex.org/f#Pump'
    assert find_iri('Pump', None) is None
    assert find_iri('', sch) is None
    assert iri_of_class('Machine', sch) == 'http://ex.org/f#Machine'
    assert iri_of_class('Machine', {'classes': {'Machine': 'http://x/M'}}) == 'http://x/M'
    assert iri_of_class('Machine', None) is None
    assert find_iri('Pump', {'entity': {'iri': 'http://e/P'}}) == 'http://e/P'
    # 不抛异常
    assert normalize_evidence({'entity': 'P'}, schema=object()) != []
    print('ok')
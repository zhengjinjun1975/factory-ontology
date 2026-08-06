#!/usr/bin/env python3
"""ontology_depth.py — 本体深度增强：补全层级结构和实体关系。

在 csv_to_owl 生成的扁平本体基础上，增加：
1. 区域层级: line.area -> Zone 类, 建 <Line_X> <locatedInZone> <Zone_车间A>
2. 设备->区域: 设备所属产线的区域 -> <Equipment_Y> <locatedIn> <Zone_车间A>
3. 类型父子类: 设备类型 subClassOf 抽象类(生产设备/动力设备/物流设备/辅助设备)

用法: python ontology_depth.py <equipment.csv> <line.csv> <ont.nt> <out.nt>
零依赖，纯标准库。
"""
import sys
import os
import csv
import re

NS = "http://factory.example/ontology#"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
OWL_CLASS = "<http://www.w3.org/2002/07/owl#Class>"
RDFS_SUBCLASS = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"
OWL_OBJPROP = "<http://www.w3.org/2002/07/owl#ObjectProperty>"
OWL_INDIV = "<http://www.w3.org/2002/07/owl#NamedIndividual>"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"


def load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    if len(sys.argv) < 5:
        print("用法: python ontology_depth.py <equipment.csv> <line.csv> <ont.nt> <out.nt>")
        sys.exit(1)
    eq_path, line_path, nt_in, nt_out = sys.argv[1:5]

    eqs = load_csv(eq_path)
    lines = load_csv(line_path) if os.path.exists(line_path) else []

    L = []

    # ---- 1. 区域类 Zone + 产线 locatedInZone 区域 ----
    L.append(f'<{NS}Zone> {RDF_TYPE} {OWL_CLASS}')
    L.append(f'<{NS}Zone> {RDFS_LABEL} "区域"')
    L.append(f'<{NS}locatedInZone> {RDF_TYPE} {OWL_OBJPROP}')
    L.append(f'<{NS}locatedInZone> <http://www.w3.org/2000/01/rdf-schema#range> <{NS}Zone>')
    L.append(f'<{NS}locatedInZone> <http://www.w3.org/2000/01/rdf-schema#domain> <{NS}Line>')
    # 产线 -> 区域
    for ln in lines:
        lid = ln.get("line_id", "").strip()
        area = ln.get("area", "").strip()
        if not lid or not area:
            continue
        line_uri = f"{NS}Line_{lid}"
        zone_uri = f"{NS}Zone_{area}"
        L.append(f'<{line_uri}> <{NS}locatedInZone> <{zone_uri}>')
        L.append(f'<{zone_uri}> {RDF_TYPE} {OWL_INDIV}')
        L.append(f'<{zone_uri}> {RDF_TYPE} <{NS}Zone>')
        L.append(f'<{zone_uri}> {RDFS_LABEL} "{area}"')
        # 产线自身属性（属性本体：产能/班次/状态）
        L.append(f'<{line_uri}> {RDF_TYPE} {OWL_INDIV}')
        L.append(f'<{line_uri}> {RDF_TYPE} <{NS}Line>')
        L.append(f'<{line_uri}> <{NS}lineName> "{ln.get("line_name","")}"')
        L.append(f'<{line_uri}> <{NS}lineSupervisor> "{ln.get("supervisor","")}"')
        L.append(f'<{line_uri}> <{NS}lineCapacity> "{ln.get("capacity_per_day","")}"')
        L.append(f'<{line_uri}> <{NS}lineShifts> "{ln.get("shift_count","")}"')
        L.append(f'<{line_uri}> <{NS}lineStatus> "{ln.get("line_status","")}"')
        L.append(f'<{line_uri}> <{NS}lineEquipmentCount> "{ln.get("equipment_count","")}"')

    # ---- 2. 设备 -> 区域（通过产线的区域）----
    line_to_area = {ln.get("line_id", "").strip(): ln.get("area", "").strip() for ln in lines}
    for i, e in enumerate(eqs, 1):
        lid = (e.get("line_id", "") or "").strip()
        did = e.get("id", "") or f"{i}"   # 与 csv_to_owl 的实例URI一致：id列 or 行号
        area = line_to_area.get(lid, "")
        if did and area:
            dev_uri = f"{NS}Equipment_{did}"
            zone_uri = f"{NS}Zone_{area}"
            L.append(f'<{dev_uri}> <{NS}locatedIn> <{zone_uri}>')

    # ---- 3. 设备类型父子类 ----
    # 设备类型 -> 抽象父类
    TYPE_PARENT = {
        "machine_tool": "生产设备", "injection_molding": "生产设备", "robot_welder": "生产设备",
        "cnc_machining": "生产设备", "welding_robot": "生产设备", "assembly_line": "生产设备",
        "compressor": "动力设备", "air_compressor": "动力设备", "power_dist": "动力设备", "cooling_unit": "动力设备",
        "conveyor": "物流设备", "agv": "物流设备",
    }
    parents = {}
    for e in eqs:
        t = (e.get("device_type", "") or e.get("Type", "")).strip()
        parent = TYPE_PARENT.get(t)
        if parent:
            parents.setdefault(t, parent)
            # 实例的类型个体（DeviceType_xxx）subClassOf 抽象类
            L.append(f'<{NS}DeviceType_{t}> {RDFS_SUBCLASS} <{NS}{parent}>')
            # 抽象父类声明
            L.append(f'<{NS}{parent}> {RDF_TYPE} {OWL_CLASS}')
            L.append(f'<{NS}{parent}> {RDFS_LABEL} "{parent}"')

    # ---- 3.5 属性本体：Manufacturer 自身属性（国家/等级/成立年份）----
    mfr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "manufacturer.csv")
    mfrs = load_csv(mfr_path) if os.path.exists(mfr_path) else []
    for m in mfrs:
        mid = m.get("manufacturer_id", "").strip()
        if not mid:
            continue
        m_uri = f"{NS}Manufacturer_{mid}"
        L.append(f'<{m_uri}> {RDF_TYPE} {OWL_INDIV}')
        L.append(f'<{m_uri}> {RDF_TYPE} <{NS}Manufacturer>')
        L.append(f'<{m_uri}> <{NS}mfrName> "{m.get("manufacturer_name","")}"')
        L.append(f'<{m_uri}> <{NS}mfrCountry> "{m.get("country","")}"')
        L.append(f'<{m_uri}> <{NS}mfrQualityGrade> "{m.get("quality_grade","")}"')
        L.append(f'<{m_uri}> <{NS}mfrFoundedYear> "{m.get("founded_year","")}"')

    # ---- 3.7 时序观测本体：Observation 观测实例（时间点/指标/数值）----
    obs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "observation.csv")
    obss = load_csv(obs_path) if os.path.exists(obs_path) else []
    if obss:
        L.append(f'<{NS}Observation> {RDF_TYPE} {OWL_CLASS}')
        L.append(f'<{NS}Observation> {RDFS_LABEL} "观测记录"')
        L.append(f'<{NS}observedAt> {RDF_TYPE} {OWL_OBJPROP}')
        L.append(f'<{NS}observedAt> <http://www.w3.org/2000/01/rdf-schema#range> <{NS}Observation>')
        L.append(f'<{NS}observedAt> <http://www.w3.org/2000/01/rdf-schema#domain> <{NS}Sensor>')
        # 设备id -> 实例URI
        eq_uri_obs = {}
        for i, e in enumerate(eqs, 1):
            did = e.get("id", "") or f"{i}"
            eq_uri_obs[e.get("device_id", "") or did] = f"{NS}Equipment_{did}"
        for ob in obss:
            oid = ob.get("observation_id", "").strip()
            sid = ob.get("sensor_id", "").strip()
            dev = ob.get("device_id", "").strip()
            if not oid:
                continue
            o_uri = f"{NS}Observation_{oid}"
            L.append(f'<{o_uri}> {RDF_TYPE} {OWL_INDIV}')
            L.append(f'<{o_uri}> {RDF_TYPE} <{NS}Observation>')
            L.append(f'<{o_uri}> <{NS}obsTimestamp> "{ob.get("timestamp","")}"')
            L.append(f'<{o_uri}> <{NS}obsMetric> "{ob.get("metric","")}"')
            L.append(f'<{o_uri}> <{NS}obsValue> "{ob.get("value","")}"')
            L.append(f'<{o_uri}> <{NS}obsUnit> "{ob.get("unit","")}"')
            # 传感器 observedAt 观测（传感器可观察到多条观测）
            if sid:
                L.append(f'<{NS}Sensor_{sid}> <{NS}observedAt> <{o_uri}>')
            # 设备 hasObservation 观测（跨实体：设备关联其时序观测）
            dev_uri = eq_uri_obs.get(dev)
            if dev_uri:
                L.append(f'<{dev_uri}> <{NS}hasObservation> <{o_uri}>')

    # ---- 4. 多实体关联：传感器 Sensor + 设备 hasSensor 传感器 ----
    sensor_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sensor.csv")
    sensors = load_csv(sensor_path) if os.path.exists(sensor_path) else []
    if sensors:
        L.append(f'<{NS}Sensor> {RDF_TYPE} {OWL_CLASS}')
        L.append(f'<{NS}Sensor> {RDFS_LABEL} "传感器"')
        L.append(f'<{NS}hasSensor> {RDF_TYPE} {OWL_OBJPROP}')
        L.append(f'<{NS}hasSensor> <http://www.w3.org/2000/01/rdf-schema#range> <{NS}Sensor>')
        L.append(f'<{NS}hasSensor> <http://www.w3.org/2000/01/rdf-schema#domain> <{NS}Equipment>')
        # 设备id -> 实例URI（与csv_to_owl一致：id列 or 行号）
        eq_uri_map = {}
        for i, e in enumerate(eqs, 1):
            did = e.get("id", "") or f"{i}"
            eq_uri_map[e.get("device_id", "") or did] = f"{NS}Equipment_{did}"
        for sn in sensors:
            sid = sn.get("sensor_id", "").strip()
            dev = sn.get("device_id", "").strip()
            if not sid:
                continue
            s_uri = f"{NS}Sensor_{sid}"
            L.append(f'<{s_uri}> {RDF_TYPE} {OWL_INDIV}')
            L.append(f'<{s_uri}> {RDF_TYPE} <{NS}Sensor>')
            L.append(f'<{s_uri}> <{NS}sensorType> "{sn.get("sensor_type","")}"')
            L.append(f'<{s_uri}> <{NS}sensorRangeMin> "{sn.get("range_min","")}"')
            L.append(f'<{s_uri}> <{NS}sensorRangeMax> "{sn.get("range_max","")}"')
            L.append(f'<{s_uri}> <{NS}sensorStatus> "{sn.get("status","")}"')
            # 设备 hasSensor 传感器
            dev_uri = eq_uri_map.get(dev)
            if dev_uri:
                L.append(f'<{dev_uri}> <{NS}hasSensor> <{s_uri}>')

    # ---- 5. 多实体关联：维护记录 Maintenance + 设备 hasMaintenance 记录 ----
    mnt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "maintenance.csv")
    mnts = load_csv(mnt_path) if os.path.exists(mnt_path) else []
    if mnts:
        L.append(f'<{NS}Maintenance> {RDF_TYPE} {OWL_CLASS}')
        L.append(f'<{NS}Maintenance> {RDFS_LABEL} "维护记录"')
        L.append(f'<{NS}hasMaintenance> {RDF_TYPE} {OWL_OBJPROP}')
        L.append(f'<{NS}hasMaintenance> <http://www.w3.org/2000/01/rdf-schema#range> <{NS}Maintenance>')
        L.append(f'<{NS}hasMaintenance> <http://www.w3.org/2000/01/rdf-schema#domain> <{NS}Equipment>')
        eq_uri_map2 = {}
        for i, e in enumerate(eqs, 1):
            did = e.get("id", "") or f"{i}"
            eq_uri_map2[e.get("device_id", "") or did] = f"{NS}Equipment_{did}"
        for m in mnts:
            mid = m.get("maintenance_id", "").strip()
            dev = m.get("device_id", "").strip()
            if not mid:
                continue
            m_uri = f"{NS}Maintenance_{mid}"
            L.append(f'<{m_uri}> {RDF_TYPE} {OWL_INDIV}')
            L.append(f'<{m_uri}> {RDF_TYPE} <{NS}Maintenance>')
            L.append(f'<{m_uri}> <{NS}maintenanceDate> "{m.get("maintenance_date","")}"')
            L.append(f'<{m_uri}> <{NS}maintenanceType> "{m.get("maintenance_type","")}"')
            L.append(f'<{m_uri}> <{NS}maintenanceHours> "{m.get("duration_hours","")}"')
            L.append(f'<{m_uri}> <{NS}maintenanceOperator> "{m.get("operator","")}"')
            L.append(f'<{m_uri}> <{NS}maintenanceCost> "{m.get("cost_yuan","")}"')
            dev_uri = eq_uri_map2.get(dev)
            if dev_uri:
                L.append(f'<{dev_uri}> <{NS}hasMaintenance> <{m_uri}>')

    # 读取原本体，追加增强
    with open(nt_out, "w", encoding="utf-8") as f:
        with open(nt_in, encoding="utf-8") as fi:
            f.write(fi.read())
        if L:
            f.write("\n# -- 本体深度增强 --\n")
            seen = set()
            for line in L:
                if line not in seen:
                    seen.add(line)
                    f.write(line + " .\n")
    print(f"✅ 深度增强完成: {nt_out}")
    print(f"   区域(Zone): {len(set(l.split(' ')[1] for l in L if 'Zone_' in l))} 个")
    print(f"   产线-区域关系: {sum(1 for l in L if 'locatedInZone' in l)} 条")
    print(f"   设备-区域关系: {sum(1 for l in L if 'locatedIn> <http://factory.example/ontology#Zone' in l)} 条")
    print(f"   类型父子类: {len(parents)} 个")
    print(f"   传感器: {len(sensors)} 个, 设备-传感器关系: {sum(1 for l in L if 'hasSensor' in l)} 条")
    print(f"   维护记录: {len(mnts)} 条, 设备-维护关系: {sum(1 for l in L if 'hasMaintenance' in l)} 条")


if __name__ == "__main__":
    main()

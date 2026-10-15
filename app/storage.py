"""Transactional import of normalized CSV cases; no external dataset dependency."""
from __future__ import annotations
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

FILES = ('vehicle_state.csv', 'object_state.csv', 'planning_state.csv', 'control_state.csv')
MAX_BYTES = 24 * 1024 * 1024
IMPORT_LOCK = threading.Lock()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def valid_case_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('Case 编号限 1～64 位字母、数字、下划线或短横线')
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]', value, re.I):
        raise ValueError('Case 编号为系统保留名称，请更换')
    return value


def validate_csv(name, text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError(name + ' 内容为空')
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or 'timestamp' not in headers or any(not h for h in headers):
        raise ValueError(name + ' 需要唯一表头和 timestamp 列')
    if name == 'vehicle_state.csv' and 'speed_mps' not in headers:
        raise ValueError('vehicle_state.csv 缺少 speed_mps')
    previous = None
    count = 0
    gaps = 0
    for row in reader:
        count += 1
        if count > 200000:
            raise ValueError(name + ' 超过 200000 行，请按片段拆分')
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f'{name} 第 {count+1} 行列数与表头不一致')
        try:
            timestamp = float(row['timestamp'])
            if not math.isfinite(timestamp):
                raise ValueError()
            if previous is not None and (timestamp < previous or name != 'object_state.csv' and timestamp == previous):
                raise ValueError()
            if name == 'vehicle_state.csv':
                speed = float(row['speed_mps'])
                if not math.isfinite(speed) or speed < 0:
                    raise ValueError()
                if previous is not None and abs(timestamp - previous - .1) > 1e-6:
                    gaps += 1
            previous = timestamp
        except (TypeError, ValueError):
            raise ValueError(f'{name} 第 {count+1} 行时间或车速无效；时间必须有序，车速单位为 m/s') from None
    if name == 'vehicle_state.csv' and count < 6:
        raise ValueError('车辆数据至少需要 6 个采样点，覆盖一个 0.5 秒窗口')
    return {'rows': count, 'sampling_gaps': gaps}


def import_case(context, payload):
    from app.annotations import _annotation_template
    if not isinstance(payload, dict) or set(payload) - {'case_id', 'source_name', 'source_type', 'acceleration_source', 'files'}:
        raise ValueError('导入字段不正确')
    cid = valid_case_id(payload.get('case_id'))
    source_name = payload.get('source_name', '')
    source_type = payload.get('source_type', 'unknown')
    acceleration_source = payload.get('acceleration_source', '')
    if not isinstance(source_type, str) or source_type not in {'recorded', 'synthetic', 'unknown'}:
        raise ValueError('来源类型无效')
    if not isinstance(source_name, str) or len(source_name) > 120 or not isinstance(acceleration_source, str) or len(acceleration_source) > 300:
        raise ValueError('来源说明过长或格式无效')
    files = payload.get('files')
    if not isinstance(files, dict) or 'vehicle_state.csv' not in files or set(files) - set(FILES):
        raise ValueError('请选择 vehicle_state.csv，可同时选择目标、规划、控制 CSV')
    if any(not isinstance(content, str) for content in files.values()) or sum(len(s.encode('utf-8')) for s in files.values()) > MAX_BYTES:
        raise ValueError('单个 Case 总文件大小不能超过 24 MB')
    stats = {name: validate_csv(name, content) for name, content in files.items()}
    root = context.data_root.parent.resolve()
    destination = context.data_root / cid
    label = context.annotations_root / (cid + '.json')
    registry_path = context.batch_dir / 'sources.json'
    index_path = context.batch_dir / 'summary.csv'
    with IMPORT_LOCK:
        existing = list(csv.DictReader(io.StringIO(index_path.read_text(encoding='utf-8-sig'))))
        if destination.exists() or label.exists() or any(row['case_id'].casefold() == cid.casefold() for row in existing):
            raise ValueError('Case 编号已存在，请使用新编号；已有数据不会覆盖')
        staging = root / ('.import-' + uuid4().hex)
        staging.mkdir()
        committed = False
        registry_old = registry_path.read_bytes() if registry_path.is_file() else None
        try:
            for name, content in files.items():
                (staging / name).write_bytes(content.encode('utf-8'))
            metadata = {'schema': 'case-import-1.0', 'case_id': cid, 'source_type': source_type,
                        'source_name': source_name, 'dataset': 'normalized-csv',
                        'imported_at': datetime.now(timezone.utc).isoformat(),
                        'source_claim_verified': False,
                        'conversion': {'acceleration_source': acceleration_source or '由数据提供方给出，测量来源未核实'},
                        'signal_sources': {'vehicle_state.csv': {'acceleration_mps2': acceleration_source}}}
            atomic_json(staging / 'source_metadata.json', metadata)
            manifest = {'files': {name: sha(staging / name) for name in (*files, 'source_metadata.json')}}
            registry = json.loads(registry_old) if registry_old else {}
            registry[cid] = manifest
            staging.rename(destination)
            template = _annotation_template(destination, context.data_root)
            atomic_json(label, template)
            atomic_json(registry_path, registry)
            output = io.StringIO(newline='')
            writer = csv.DictWriter(output, fieldnames=['case_id','source_route','source_segment'])
            writer.writeheader()
            writer.writerows(existing + [{'case_id': cid, 'source_route': source_name, 'source_segment': ''}])
            temporary = index_path.with_suffix('.csv.tmp')
            temporary.write_text(output.getvalue(), encoding='utf-8')
            temporary.replace(index_path)
            committed = True
        finally:
            if not committed:
                # Only paths created by this transaction can be removed.
                for owned in (staging, destination):
                    if owned.exists() and owned.resolve().parent in {root, context.data_root.resolve()} and not owned.is_symlink():
                        shutil.rmtree(owned)
                label.unlink(missing_ok=True)
                if registry_old is not None:
                    atomic_json(registry_path, json.loads(registry_old))
                else:
                    registry_path.unlink(missing_ok=True)
    warnings = []
    if stats['vehicle_state.csv']['sampling_gaps']:
        warnings.append('车速采样存在缺口或不符合 0.1 秒网格，候选将保留数据质量疑点')
    if source_type == 'unknown':
        warnings.append('来源未声明，候选需人工核查来源')
    return {'ok': True, 'case_id': cid, 'files': stats, 'warnings': warnings,
            'workbench_url': '/workbench/?case=' + cid}


def source_quality(case_dir, protocol, grid, csv_speed):
    result = {'verified': False, 'issues': [], 'gaps': [], 'metadata_sha256': None,
              'original_can_sha256': None, 'source_type': 'imported',
              'source_label': '导入日志；校验范围为文件完整性和采样，采集真实性由提供方声明'}
    try:
        metadata_path = case_dir / 'source_metadata.json'
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        result['metadata_sha256'] = sha(metadata_path)
        registry = json.loads((case_dir.parent.parent / 'index/sources.json').read_text(encoding='utf-8'))
        manifest = registry[case_dir.name]['files']
        for name in ('vehicle_state.csv', 'source_metadata.json'):
            if sha(case_dir / name) != manifest[name]:
                raise ValueError('导入后的车辆日志或来源说明发生变化，请使用新编号重新导入')
        if metadata.get('schema') != 'case-import-1.0' or metadata.get('case_id') != case_dir.name:
            raise ValueError('导入来源记录无效')
        if metadata.get('source_type') not in {'recorded', 'synthetic'}:
            raise ValueError('日志来源尚未声明')
        if metadata['source_type'] == 'synthetic':
            result['source_type'] = 'synthetic'
            result['source_label'] = '合成数据；已核对导入完整性，非真实道路采集'
        result['source_sha256'] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        result['verified'] = True
    except (OSError, ValueError, KeyError, TypeError) as error:
        result['issues'].append('来源核查：' + str(error))
    return result

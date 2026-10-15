"""Stored and on-demand model advice for the annotation workbench."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from app.candidate_review import MERGE_PATH, build_candidate_case, create_candidate_adapter, _raw_record
from app.model_runner import get_model_call_metrics, reset_model_call_metrics
from app.candidate_decision import VERSION, build_decision_prompt, validate_decision, route_decision
from app.candidate_context import enrich_case
from app.annotation_protocol import PROTOCOL_ID
from app.event_investigation import (VERSION as INVESTIGATION_VERSION, build_investigation,
    build_investigation_prompt, render_investigation, source_hashes, validate_investigation)


class ReviewBusyError(ValueError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(case: dict, merge_digest: str) -> str:
    source = case["quality"]["original_can"]
    value = {"vehicle": case["vehicle_sha256"], "protocol": case["annotation_protocol_sha256"],
             "metadata": source.get("metadata_sha256"), "native": source.get("original_can_sha256"),
             "screening": case["screening_config"], "merge": merge_digest,
             "candidates": [{k: v for k, v in c.items() if k not in {"review", "vehicle_csv", "disposition", "investigation_available"}} for c in case["candidates"]]}
    if "context_source_hashes" in case:
        value["context_sources"] = case["context_source_hashes"]
    if source.get('source_type') == 'synthetic':
        value['synthetic_source'] = source.get('source_sha256')
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("模型记录格式无效")
    return value


def _read_candidate_record(path: Path) -> dict:
    record = _read(path)
    saved = record.get('candidate')
    signature = record.get('input_fingerprint')
    if (not isinstance(saved, dict) or not isinstance(saved.get('review'), dict)
            or not isinstance(signature, str) or not re.fullmatch(r'[a-f0-9]{64}', signature)
            or any(type(saved.get(key)) not in {int, float} or not math.isfinite(saved[key])
                   for key in ('start_time', 'end_time'))):
        raise ValueError('已有候选复核记录格式无效，请检查本地文件；原记录未覆盖')
    return record


class WorkbenchReviewStore:
    def __init__(self, summary_path: Path | None, output_root: Path, adapter_factory=None, annotations_root: Path | None = None, manual_mode=False):
        self.manual_mode = manual_mode
        self.summary_path = summary_path
        self.output_root = output_root
        self.adapter_factory = adapter_factory
        self.annotations_root = annotations_root
        self.lock = threading.Lock()

    def saved_case_ids(self) -> set[str]:
        result = set()
        if self.output_root.is_dir():
            for directory in self.output_root.iterdir():
                if not directory.is_dir():
                    continue
                for path in directory.glob("C*.json"):
                    try:
                        if _read_candidate_record(path)["candidate"]["review"].get("prompt_version") == VERSION:
                            result.add(directory.name)
                            break
                    except (OSError, ValueError, KeyError):
                        continue
        return result

    def get_case(self, case_dir: Path) -> dict:
        case = enrich_case(case_dir, build_candidate_case(case_dir, include_module_evidence=False))
        signature = _signature(case, _digest(MERGE_PATH))
        case["input_fingerprint"] = signature
        for candidate in case["candidates"]:
            candidate["review"] = {"status": "not_requested", "adapter": None, "output": None}
            cached = self.output_root / case_dir.name / (candidate["candidate_id"] + ".json")
            saved = None
            if cached.is_file():
                record = _read_candidate_record(cached)
                saved = record['candidate']
                record_signature = record['input_fingerprint']
            if saved and saved.get("review", {}).get("prompt_version") == VERSION and saved["review"].get("status") != "not_requested":
                same_interval = abs(saved["start_time"] - candidate["start_time"]) < 1e-6 and abs(saved["end_time"] - candidate["end_time"]) < 1e-6
                if record_signature != signature or not same_interval:
                    candidate["review"] = {"status": "stale", "adapter": None, "output": None}
                else:
                    review = dict(saved["review"])
                    # Recheck a raw response rejected by an older validator, using
                    # current facts and schema. This never issues a model call.
                    if review.get("status") == "needs_manual_review" and record.get("raw_response") is not None:
                        try:
                            review["output"] = validate_decision(record["raw_response"], build_decision_prompt(candidate))
                            review.update(status="validated", error_type=None, revalidated=True)
                        except ValueError:
                            pass
                    if review.get("output") is not None:
                        try:
                            validate_decision(review["output"], build_decision_prompt(candidate))
                        except ValueError:
                            review = {"status": "needs_manual_review", "adapter": review.get("adapter"), "output": None, "error_type": "StoredReviewValidationError"}
                    candidate["review"] = review
        for candidate in case["candidates"]:
            candidate["disposition"] = route_decision(candidate)
            if self.manual_mode and candidate['disposition']['status'] == 'pending_ai':
                candidate['disposition'] = {'status': 'needs_human', 'reason_code': 'manual_mode',
                    'reason': 'AI 未配置，当前由人工核查此候选', 'confirmed_intervals': [],
                    'event_protocol_id': PROTOCOL_ID}
            human_path = self.output_root / case_dir.name / "human" / (candidate["candidate_id"] + ".json")
            annotation_path = self.annotations_root / (case_dir.name + ".json") if self.annotations_root else None
            if human_path.is_file() and annotation_path and annotation_path.is_file():
                human = _read(human_path)
                if human.get("input_fingerprint") == signature and human.get("annotation_sha256") == _digest(annotation_path):
                    candidate["disposition"] = human["disposition"]
            candidate["investigation_available"] = (self.output_root / case_dir.name / "investigations" / (candidate["candidate_id"] + ".json")).is_file()
        case["reviewed_count"] = sum(c["review"]["status"] == "validated" for c in case["candidates"])
        case["triage_summary"] = {status: sum(c["disposition"]["status"] == status for c in case["candidates"])
            for status in ("ai_confirmed_event", "ai_rejected_candidate", "needs_human", "pending_ai", "human_reviewed")}
        return case

    def _confirmed_candidate(self, case_dir, candidate_id, expected_fingerprint=None):
        case = self.get_case(case_dir)
        if expected_fingerprint is not None and case['input_fingerprint'] != expected_fingerprint:
            raise ValueError('数据或口径已变化，请重新打开 Case')
        candidate = next((c for c in case['candidates'] if c['candidate_id']==candidate_id),None)
        if candidate is None:
            raise ValueError('候选编号不存在')
        if candidate['disposition']['status'] != 'ai_confirmed_event':
            raise ValueError('仅已通过校验的 AI 确认事件可自动排查')
        return case,candidate

    def get_investigation(self, case_dir, candidate_id):
        case,candidate = self._confirmed_candidate(case_dir,candidate_id)
        path = self.output_root / case_dir.name / 'investigations' / (candidate_id+'.json')
        if not path.is_file():
            return {'status':'not_requested','report':None}
        record = _read(path)
        bundle = record.get('bundle')
        if (not isinstance(bundle, dict) or not isinstance(bundle.get('events'), list)
                or any(not isinstance(event, dict) or 'confirmed_interval' not in event
                       for event in bundle['events'])):
            raise ValueError('已有排查报告格式无效，请检查本地文件；原记录未覆盖')
        fingerprint = hashlib.sha256(json.dumps({k:v for k,v in bundle.items() if k!='input_fingerprint'},sort_keys=True,allow_nan=False).encode()).hexdigest()
        if (record.get('status') == 'stale' or bundle.get('input_fingerprint') != fingerprint
            or bundle.get('version') != INVESTIGATION_VERSION or bundle.get('review_fingerprint') != case['input_fingerprint']
            or bundle.get('input_hashes') != source_hashes(case_dir)
            or [e['confirmed_interval'] for e in bundle['events']] != candidate['disposition']['confirmed_intervals']):
            return {'status':'stale','report':None}
        if record.get('model_output') is not None:
            try:
                validate_investigation(record['model_output'],build_investigation_prompt(bundle))
            except ValueError:
                record = dict(record, status='model_failed',model_output=None,error_type='StoredInvestigationValidationError')
        record['report'] = render_investigation(bundle,record.get('model_output'))
        return record

    def investigate(self, case_dir, candidate_id, expected_fingerprint, use_model=False):
        if not isinstance(expected_fingerprint,str) or not expected_fingerprint:
            raise ValueError('缺少当前事件输入指纹')
        if type(use_model) is not bool:
            raise ValueError('AI 排查参数必须为布尔值')
        if not self.lock.acquire(blocking=False):
            raise ReviewBusyError('已有请求正在运行，请稍后再试')
        try:
            case,candidate = self._confirmed_candidate(case_dir,candidate_id,expected_fingerprint)
            if not use_model:
                existing = self.get_investigation(case_dir,candidate_id)
                if existing['status'] in {'local_ready','model_validated','model_failed'}:
                    return existing
            provenance = candidate['supplemental_context']['provenance']
            bundle = build_investigation(case_dir,candidate['disposition']['confirmed_intervals'],candidate_id,
                case['input_fingerprint'],provenance)
            package = build_investigation_prompt(bundle)
            raw,output,metrics,error_type = None,None,None,None
            status,started = 'local_ready',perf_counter()
            if use_model:
                adapter = (self.adapter_factory or create_candidate_adapter)()
                reset_model_call_metrics(adapter)
                try:
                    raw = adapter.generate(package)
                    output = validate_investigation(raw,package)
                    status = 'model_validated'
                except Exception as error:
                    status,error_type = 'model_failed',type(error).__name__
                metrics = get_model_call_metrics(adapter)
            try:
                self._confirmed_candidate(case_dir,candidate_id,expected_fingerprint)
                changed = bundle['input_hashes'] != source_hashes(case_dir)
            except (OSError,ValueError):
                changed = True
            if changed:
                status,error_type,output = 'stale','InputChangedDuringInvestigation',None
            record = {'version':INVESTIGATION_VERSION,'status':status,'bundle':bundle,
                'model_output':output,'prompt':package if use_model else None,'raw_response':_raw_record(raw),
                'model_call_metrics':metrics,'error_type':error_type,'latency_seconds':round(perf_counter()-started,6),
                'report':None if changed else render_investigation(bundle,output),
                'created_at':datetime.now(timezone.utc).isoformat()}
            directory = self.output_root / case_dir.name / 'investigations'
            directory.mkdir(parents=True,exist_ok=True)
            target = directory / (candidate_id+'.json')
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(record,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
            temporary.replace(target)
            return record
        finally:
            self.lock.release()

    def resolve_human(self, case_dir: Path, candidate_id: str, expected_fingerprint: str) -> dict:
        if not self.lock.acquire(blocking=False):
            raise ReviewBusyError("已有请求正在运行，请稍后再试")
        try:
            case = self.get_case(case_dir)
            if case["input_fingerprint"] != expected_fingerprint:
                raise ValueError("数据或口径已变化，请重新打开 Case")
            candidate = next((c for c in case["candidates"] if c["candidate_id"] == candidate_id), None)
            if candidate is None or candidate["disposition"]["status"] != "needs_human":
                raise ValueError("候选不在待人工队列")
            if not self.annotations_root:
                raise ValueError("未配置人工标注目录")
            path = self.annotations_root / (case_dir.name + ".json")
            annotation_sha = _digest(path)
            annotation = _read(path)
            if annotation.get("annotation_status") != "completed" or annotation.get("case_label") not in {"positive", "negative"}:
                raise ValueError("请先在右侧保存已完成的人工标注")
            source = case["quality"]["original_can"]
            expected = {"vehicle_sha256":case["vehicle_sha256"], "protocol_sha256":case["annotation_protocol_sha256"],
                "metadata_sha256":source.get("metadata_sha256"), "original_can_sha256":source.get("original_can_sha256")}
            confirmation = annotation.get("review_confirmation") or {}
            if (annotation.get("annotation_protocol_id") != PROTOCOL_ID or confirmation.get("protocol_id") != PROTOCOL_ID
                    or not case["quality"]["can_complete"] or confirmation.get("whole_case_reviewed") is not True
                    or any(confirmation.get(k) != v for k,v in expected.items())):
                raise ValueError("人工确认与当前来源不一致，请重新核查并保存")
            if candidate["boundary_review_required"] and confirmation.get("boundary_reviewed") is not True:
                raise ValueError("请先确认相邻片段边界")
            protocol = annotation.get("annotation_protocol_id", "unknown")
            disposition = {"status":"human_reviewed", "policy_id":"candidate-routing-1.0", "reason_code":"explicit_human_resolution",
                "reason":"人工已处理；依据已完成的 " + protocol + " 标注", "confirmed_intervals":[], "event_protocol_id":protocol}
            if _digest(path) != annotation_sha or _signature(enrich_case(case_dir, build_candidate_case(case_dir, include_module_evidence=False)), _digest(MERGE_PATH)) != expected_fingerprint:
                raise ValueError("数据或人工标注已变化，请重新打开 Case")
            record = {"input_fingerprint":expected_fingerprint, "annotation_sha256":annotation_sha,
                "candidate_id":candidate_id, "case_id":case_dir.name, "disposition":disposition,
                "annotation_case_label":annotation["case_label"], "annotator":annotation.get("annotator"),
                "resolved_at":datetime.now(timezone.utc).isoformat()}
            directory = self.output_root / case_dir.name / "human"
            directory.mkdir(parents=True, exist_ok=True)
            content = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
            history = directory / "history"
            history.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            (history / (candidate_id + "_" + stamp + ".json")).write_text(content, encoding="utf-8")
            target = directory / (candidate_id + ".json")
            temporary = target.with_suffix(".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
            return self.get_case(case_dir)
        finally:
            self.lock.release()

    def review(self, case_dir: Path, candidate_id: str, expected_fingerprint: str, *, only_pending=False) -> dict:
        if not self.lock.acquire(blocking=False):
            raise ReviewBusyError("已有模型请求正在运行，请稍后再试")
        try:
            case = self.get_case(case_dir)
            if expected_fingerprint != case["input_fingerprint"]:
                raise ValueError("数据或口径已变化，请重新打开 Case")
            current_candidate = next((c for c in case['candidates'] if c['candidate_id'] == candidate_id), None)
            if only_pending and current_candidate and current_candidate['disposition']['status'] != 'pending_ai':
                return case
            if not case["quality"]["can_complete"]:
                raise ValueError("请先核查数据来源和质量")
            candidate = next((c for c in case["candidates"] if c["candidate_id"] == candidate_id), None)
            if candidate is None:
                raise ValueError("候选编号不存在")
            package = build_decision_prompt(candidate)
            adapter = (self.adapter_factory or create_candidate_adapter)()
            reset_model_call_metrics(adapter)
            started, raw, output = perf_counter(), None, None
            try:
                raw = adapter.generate(package)
            except Exception as error:
                status, error_type = "call_failed", type(error).__name__
            else:
                try:
                    output = validate_decision(raw, package)
                    status, error_type = "validated", None
                except Exception as error:
                    status, error_type = "needs_manual_review", type(error).__name__
            try:
                after = enrich_case(case_dir, build_candidate_case(case_dir, include_module_evidence=False))
                changed = _signature(after, _digest(MERGE_PATH)) != case["input_fingerprint"]
            except (OSError, ValueError):
                changed = True
            if changed:
                status, error_type, output = "stale", "InputChangedDuringReview", None
            candidate["review"] = {"status": status, "adapter": "deepseek", "output": output, "prompt_version": VERSION,
                "error_type": error_type, "latency_seconds": round(perf_counter() - started, 6),
                "call_metrics": get_model_call_metrics(adapter)}
            candidate["disposition"] = route_decision(candidate)
            if self.manual_mode and candidate['disposition']['status'] == 'pending_ai':
                candidate['disposition'] = {'status': 'needs_human', 'reason_code': 'manual_mode',
                    'reason': 'AI 未配置，当前由人工核查此候选', 'confirmed_intervals': [],
                    'event_protocol_id': PROTOCOL_ID}
            record = {"version": "2.1", "case_id": case_dir.name, "input_fingerprint": case["input_fingerprint"],
                "created_at": datetime.now(timezone.utc).isoformat(), "candidate": candidate,
                "prompt": package, "raw_response": _raw_record(raw)}
            directory = self.output_root / case_dir.name
            directory.mkdir(parents=True, exist_ok=True)
            content = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            target = directory / (candidate_id + ".json")
            temporary = directory / (candidate_id + ".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
            return self.get_case(case_dir)
        finally:
            self.lock.release()

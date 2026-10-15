"""Frozen synthetic evaluation, with human labels isolated from AI decisions."""
import hashlib
import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.annotation_assist import build_annotation_assist
from app.paths import PROJECT_ROOT
from app.storage import atomic_json as _atomic_json
from app.evaluation_resources import archive_resources, resource_base


class EvaluationConflict(ValueError):
    pass


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    _atomic_json(path,value)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def read(path):
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):
        raise ValueError('评估记录格式无效')
    return value


def iou(a,b):
    intersection=max(0,min(a[1],b[1])-max(a[0],b[0]))
    union=max(a[1],b[1])-min(a[0],b[0])
    return intersection/union if union else 0


def match_events(predicted,truth,threshold=.5):
    """Maximum-cardinality one-to-one matching. Duplicates remain false positives."""
    edges=[sorted((j for j,gold in enumerate(truth) if iou(event,gold)>=threshold-1e-9),
                  key=lambda j:(-iou(event,truth[j]),j)) for event in predicted]
    matched={}
    def augment(index,seen):
        for gold in edges[index]:
            if gold in seen:
                continue
            seen.add(gold)
            if gold not in matched or augment(matched[gold],seen):
                matched[gold]=index
                return True
        return False
    for index in range(len(predicted)):
        augment(index,set())
    return sorted((pred,gold) for gold,pred in matched.items())


def coverage(event,candidates):
    parts=sorted((max(event[0],a),min(event[1],b)) for a,b in candidates if min(event[1],b)>max(event[0],a))
    merged=[]
    for start,end in parts:
        if merged and start<=merged[-1][1]:
            merged[-1][1]=max(merged[-1][1],end)
        else:
            merged.append([start,end])
    return sum(b-a for a,b in merged)/(event[1]-event[0])


def ratio(a,b):
    return a/b if b else None


def score(snapshot,labels,ratings,rules):
    tp=fp=fn=gold_count=covered=rejected_gold=0
    total_candidates=automatic_candidates=0
    evaluated=excluded=0
    cases=[]
    for cid,case in snapshot['cases'].items():
        candidates=case['candidates']
        total_candidates+=len(candidates)
        automatic_candidates+=sum(c['status'] in {'ai_confirmed_event','ai_rejected_candidate'} for c in candidates)
        label=labels[cid]
        if label['decision'] not in {'positive','negative'}:
            excluded+=1
            cases.append({'case_id':cid,'excluded':True,'reason':label['decision']})
            continue
        evaluated+=1
        gold=label['events']
        predictions=[interval for c in candidates if c['status']=='ai_confirmed_event' for interval in c['confirmed_intervals']]
        matched=match_events(predictions,gold,rules['event_iou'])
        covered_ids=[i for i,event in enumerate(gold) if coverage(event,[c['interval'] for c in candidates])>=rules['candidate_coverage']-1e-9]
        rejection_ids=[i for i,event in enumerate(gold) if any(
            coverage(event,[c['interval']])>=rules['rejection_coverage']-1e-9 for c in candidates if c['status']=='ai_rejected_candidate')]
        local_tp=len(matched); local_fp=len(predictions)-local_tp; local_fn=len(gold)-local_tp
        tp+=local_tp; fp+=local_fp; fn+=local_fn; covered+=len(covered_ids); gold_count+=len(gold); rejected_gold+=len(rejection_ids)
        cases.append({'case_id':cid,'excluded':False,'gold_events':gold,'predicted_events':predictions,
            'tp':local_tp,'fp':local_fp,'fn':local_fn,'candidate_missed_events':[event for i,event in enumerate(gold) if i not in covered_ids],
            'wrongly_rejected_events':[event for i,event in enumerate(gold) if i in rejection_ids],
            'boundary_errors':[{'prediction':p,'truth':g,'start_error_seconds':abs(predictions[p][0]-gold[g][0]),
                'end_error_seconds':abs(predictions[p][1]-gold[g][1])} for p,g in matched]})
    assessed=[value for value in ratings.values() if value.get('completed')]
    useful=sum(all(r['checks'].values()) for r in assessed)
    return {'evaluated_cases':evaluated,'excluded_cases':excluded,'gold_event_count':gold_count,
        'candidate_recall':ratio(covered,gold_count),'candidate_covered_events':covered,
        'automatic_confirmation_precision':ratio(tp,tp+fp),'automatic_event_recall':ratio(tp,tp+fn),
        'tp':tp,'fp':fp,'fn':fn,'wrongly_rejected_gold_events':rejected_gold,
        'automatic_processing_coverage':ratio(automatic_candidates,total_candidates),
        'automatic_candidates':automatic_candidates,'total_candidates':total_candidates,
        'report_useful_rate':ratio(useful,len(assessed)),'reports_assessed':len(assessed),
        'reports_total':len(snapshot['reports']),'reports_useful':useful,
        'model_reports_validated':sum(r.get('status','model_validated')=='model_validated' for r in snapshot['reports'].values()),
        'model_reports_failed':sum(r.get('status')=='model_failed' for r in snapshot['reports'].values()),'cases':cases}


class EvaluationStore:
    def __init__(self,context):
        self.context=context
        self.root=context.evaluation_output
        self.plan_path=self.root/'benchmark.json'
        self.lock=threading.RLock()

    @property
    def enabled(self):
        return self.plan_path.is_file()

    def plan(self):
        if not self.enabled:
            raise ValueError('当前工作空间没有效果评估批次')
        plan=read(self.plan_path)
        if plan.get('version')!=1 or not isinstance(plan.get('case_ids'),list):
            raise ValueError('评估批次格式无效')
        return plan

    def state(self):
        path=self.root/'evaluation_state.json'
        return read(path) if path.exists() else {'unblinded':False}

    def valid(self,plan):
        for name,expected in plan['frozen_files'].items():
            base=resource_base(self.root,plan) if name.startswith('@app/') else self.context.data_root.parent
            path=(base/name.removeprefix('@app/')).resolve()
            if not path.is_relative_to(base.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                raise EvaluationConflict('评估输入、规则或模型结果已变化，请建立新批次')

    def label(self,cid):
        self.member(cid)
        path=self.root/'human_labels'/f'{cid}.json'
        value=read(path) if path.exists() else {'case_id':cid,'completed':False,'decision':'','events':[],
            'reviewer':'','notes':'','whole_case_reviewed':False,'post_unblind_edit':False}
        return dict(value,revision=digest(value))

    def member(self,cid):
        if not isinstance(cid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',cid) or cid not in self.plan()['case_ids']:
            raise ValueError('Case 不在评估批次中')

    def snapshot(self):
        return read(self.root/'predictions.json')

    def rating(self,key):
        if key not in self.snapshot()['reports']:
            raise ValueError('报告不在本次评估中')
        path=self.root/'report_ratings'/f'{key}.json'
        value=read(path) if path.exists() else {'completed':False,'reviewer':'','notes':'','checks':{}}
        return dict(value,revision=digest(value))

    def summary(self):
        if not self.enabled:
            return {'enabled':False}
        plan=self.plan(); self.valid(plan)
        labels={cid:self.label(cid) for cid in plan['case_ids']}
        unblinded=self.state()['unblinded']
        result={'enabled':True,'unblinded':unblinded,'name':plan['name'],'seed':plan['seed'],
            'protocol_id':plan['protocol_id'],'total':len(labels),'completed':sum(label['completed'] for label in labels.values()),
            'cases':[{'case_id':cid,'completed':label['completed'],'decision':label['decision']} for cid,label in labels.items()],
            'metric_rules':plan['metric_rules'],'metrics':None,'reports':[]}
        if unblinded:
            snapshot=self.snapshot(); ratings={key:self.rating(key) for key in snapshot['reports']}
            result['metrics']=score(snapshot,labels,ratings,plan['metric_rules'])
            result['post_unblind_edits']=sum(label.get('post_unblind_edit',False) for label in labels.values())
            result['reports']=[{'key':key,'case_id':report['case_id'],'candidate_id':report['candidate_id'],
                'completed':ratings[key]['completed']} for key,report in snapshot['reports'].items()]
        return result

    def case(self,cid):
        plan=self.plan(); self.valid(plan); self.member(cid)
        assist=build_annotation_assist(self.context.case_dir(cid))
        value={'case_id':cid,'points':assist['points'],'quality':{
            'can_complete':assist['quality']['can_complete'],'issues':assist['quality']['issues']},
            'label':self.label(cid),'unblinded':self.state()['unblinded']}
        if value['unblinded']:
            value['prediction']=self.snapshot()['cases'][cid]
        return value

    def save_label(self,cid,body):
        with self.lock:
            plan=self.plan(); self.valid(plan); current=self.label(cid)
            if body.get('expected_revision')!=current['revision']:
                raise EvaluationConflict('标注已在其他页面更新，请重新读取')
            if body.get('whole_case_reviewed') is not True:
                raise ValueError('请确认已独立查看整段曲线')
            reviewer=body.get('reviewer'); notes=body.get('notes',''); decision=body.get('decision')
            if not isinstance(reviewer,str) or not 1<=len(reviewer.strip())<=80 or not isinstance(notes,str) or len(notes)>2000:
                raise ValueError('请填写核对人，并保持备注在 2000 字以内')
            if decision not in {'positive','negative','uncertain','data_issue'}:
                raise ValueError('请选择事件结论')
            events=body.get('events')
            if not isinstance(events,list) or len(events)>100:
                raise ValueError('事件区间格式无效')
            assist=build_annotation_assist(self.context.case_dir(cid))
            points=assist['points']; lower=points[0]['timestamp']; upper=points[-1]['timestamp']
            for interval in events:
                if (not isinstance(interval,list) or len(interval)!=2
                    or any(type(v) not in {int,float} or not math.isfinite(v) for v in interval)
                    or interval[0]<lower-1e-6 or interval[1]>upper+1e-6 or interval[1]-interval[0]<.5-1e-6):
                    raise ValueError('区间须在日志范围内，且至少覆盖 0.5 秒')
            events=sorted(events)
            if any(a[1]>b[0]+1e-6 for a,b in zip(events,events[1:])):
                raise ValueError('区间不能重叠，请核对是否应合并')
            if bool(events)!=(decision=='positive'):
                raise ValueError('有事件需填写区间，其他结论请清空区间')
            if decision in {'positive','negative'} and not assist['quality']['can_complete']:
                raise ValueError('数据质量不足，请选择数据不足或无法判断')
            if decision in {'uncertain','data_issue'} and not notes.strip():
                raise ValueError('无法判断或数据不足时请简述原因')
            after=self.state()['unblinded']
            if after and not notes.strip():
                raise ValueError('对照后的修订需要填写原因')
            value={'case_id':cid,'completed':True,'decision':decision,'events':events,
                'reviewer':reviewer.strip(),'notes':notes.strip(),'whole_case_reviewed':True,
                'post_unblind_edit':after or current.get('post_unblind_edit',False),
                'updated_at':datetime.now(timezone.utc).isoformat(),'label_source':'human_independent_review'}
            if current['completed']:
                atomic_json(self.root/'label_history'/f'{cid}_{current["revision"]}.json',{k:v for k,v in current.items() if k!='revision'})
            atomic_json(self.root/'human_labels'/f'{cid}.json',value)
            return dict(value,revision=digest(value))

    def unblind(self):
        with self.lock:
            plan=self.plan(); self.valid(plan)
            if not all(self.label(cid)['completed'] for cid in plan['case_ids']):
                raise EvaluationConflict('请先核对全部 Case，再开启结果对照')
            if not self.state()['unblinded']:
                atomic_json(self.root/'blind_labels.json',{cid:self.label(cid) for cid in plan['case_ids']})
                atomic_json(self.root/'evaluation_state.json',{'unblinded':True,'opened_at':datetime.now(timezone.utc).isoformat()})
            return self.summary()

    def report(self,key):
        plan=self.plan(); self.valid(plan)
        if not self.state()['unblinded']:
            raise EvaluationConflict('先完成独立事件核对，再查看排查报告')
        if key not in self.snapshot()['reports']:
            raise ValueError('报告不在评估批次中')
        return dict(self.snapshot()['reports'][key],rating=self.rating(key))

    def save_rating(self,key,body):
        with self.lock:
            report=self.report(key); current=report['rating']
            if body.get('expected_revision')!=current['revision']:
                raise EvaluationConflict('报告评价已在其他页面更新，请重新读取')
            checks=body.get('checks'); reviewer=body.get('reviewer'); notes=body.get('notes','')
            if not isinstance(checks,dict) or set(checks)!={'evidence_correct','actionable','no_unsupported_claim'} or any(type(v) is not bool for v in checks.values()):
                raise ValueError('请逐项评价证据、建议和结论')
            if not isinstance(reviewer,str) or not 1<=len(reviewer.strip())<=80 or not isinstance(notes,str) or len(notes)>2000:
                raise ValueError('请填写评价人及有效备注')
            if not all(checks.values()) and not notes.strip():
                raise ValueError('有问题的报告请简述原因')
            value={'completed':True,'reviewer':reviewer.strip(),'notes':notes.strip(),'checks':checks,
                'report_sha256':report['report_sha256'],'updated_at':datetime.now(timezone.utc).isoformat()}
            atomic_json(self.root/'report_ratings'/f'{key}.json',value)
            return dict(value,revision=digest(value))


def freeze(context,snapshot,seed,generator_hashes):
    root=context.evaluation_output
    if (root/'benchmark.json').exists():
        raise ValueError('评估批次已冻结，不能覆盖')
    atomic_json(root/'predictions.json',snapshot)
    files={}
    for cid in snapshot['cases']:
        for path in context.case_dir(cid).iterdir():
            if path.is_file():
                files[str(path.relative_to(context.data_root.parent)).replace('\\','/')]=hashlib.sha256(path.read_bytes()).hexdigest()
    for path in context.model_reviews_root.rglob('*.json'):
        if path.name!='automatic_queue.json':
            files[str(path.relative_to(context.data_root.parent)).replace('\\','/')]=hashlib.sha256(path.read_bytes()).hexdigest()
    for path in context.batch_dir.glob('*.json'):
        files[str(path.relative_to(context.data_root.parent)).replace('\\','/')]=hashlib.sha256(path.read_bytes()).hexdigest()
    index_path=context.batch_dir/'summary.csv'
    files[str(index_path.relative_to(context.data_root.parent)).replace('\\','/')]=hashlib.sha256(index_path.read_bytes()).hexdigest()
    snapshot_path=root/'predictions.json'
    files[str(snapshot_path.relative_to(context.data_root.parent)).replace('\\','/')]=hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    for path in (PROJECT_ROOT/'configs').glob('*.json'):
        if path.name not in {'deepseek.local.json','deepseek.example.json','workspace.json'}:
            files['@app/'+str(path.relative_to(PROJECT_ROOT)).replace('\\','/')]=hashlib.sha256(path.read_bytes()).hexdigest()
    for path in (PROJECT_ROOT/'knowledge').iterdir():
        if path.is_file():
            files['@app/'+str(path.relative_to(PROJECT_ROOT)).replace('\\','/')]=hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('annotation_assist','annotation_protocol','candidate_review','candidate_decision','candidate_context',
                 'event_investigation','model_runner','workbench_reviews','human_event_review','evaluation'):
        path=PROJECT_ROOT/'app'/f'{name}.py'
        files['@app/'+str(path.relative_to(PROJECT_ROOT)).replace('\\','/')]=hashlib.sha256(path.read_bytes()).hexdigest()
    plan={'version':1,'name':'独立效果评估','seed':seed,'protocol_id':'SD-WINDOW-1.1',
        'created_at':datetime.now(timezone.utc).isoformat(),'case_ids':list(snapshot['cases']),
        'generator_hashes':generator_hashes,'frozen_files':files,
        'metric_rules':{'event_iou':.5,'candidate_coverage':.9,'rejection_coverage':.5}}
    atomic_json(root/'benchmark.json',plan)
    return archive_resources(root,plan)

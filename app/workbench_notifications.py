"""Durable, coalesced Feishu side channel; model work never waits for HTTP."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import quote, urlsplit

from app.notify import load_config, send_card
from app.workbench_overview import review_task

STATES = ('pending', 'sending', 'sent', 'failed', 'unknown', 'superseded')


def progress_card(events, base_url):
    counts = {key:sum(e['status'] == key for e in events)
              for key in ('done','manual','failed','changed','data_issue')}
    attention = counts['manual'] + counts['data_issue']
    lines = [f"本次更新 {len(events)} 个 Case · 自动处理结束 {counts['done']} · 待人工 {attention} · 失败/变更 {counts['failed']+counts['changed']}",
             f"确认事件区间 {sum(e['event_count'] for e in events)} 段 · AI 排查报告 {sum(e['reports'] for e in events)} 份"]
    labels = {'done':'自动处理结束', 'manual':'待人工复核', 'failed':'处理失败，检查后重试',
              'changed':'数据已变更，需重新处理', 'data_issue':'数据待核查'}
    for event in events:
        url = base_url.rstrip('/') + '/workbench/?case=' + quote(event['case_id'], safe='')
        lines.append(f"- [{event['case_id']}]({url})：{labels[event['status']]}"
                     + (f" · {event['manual']} 个候选待人工" if event['manual'] else ''))
    if any(e['paused'] for e in events):
        lines.append('AI 队列已暂停：检查模型密钥、余额或权限后，再重试并继续队列。')
    if urlsplit(base_url).hostname in {'127.0.0.1','localhost','::1'}:
        lines.append('工作台链接仅在运行服务的电脑上可用。')
    lines.append('报告提供排查建议，仍需工程师执行核查。')
    return ('case-analyzer · 处理进展', lines, 'case-analyzer · 自动流程',
            'red' if counts['failed'] else 'orange' if attention or counts['changed'] else 'blue')


class WorkbenchNotifications:
    def __init__(self, path, reviews, resolve_case, config=None, sender=None):
        self.path = Path(path)
        self.reviews, self.resolve_case = reviews, resolve_case
        self.config = load_config() if config is None else dict(config)
        self.sender = sender or send_card
        self.error = self.config.get('error')
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = False
        self.worker = None
        self.base_url = self.config.get('workbench_url', '')
        self.enabled = self.config['enabled'] and not self.error
        if self.enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self._db() as db:
                    db.execute('CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, case_id TEXT NOT NULL, '
                               'payload TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, warning TEXT)')
                    # A crash after the remote server accepted a request is ambiguous.
                    db.execute("UPDATE notices SET state='unknown', warning='发送中断，送达情况未知；重发前请检查飞书群' WHERE state='sending'")
            except (OSError, sqlite3.Error):
                self.error = '通知记录无法读取或保存，请检查运行目录'

    @contextmanager
    def _db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=2)
            try:
                with db:
                    yield db
            finally:
                db.close()

    def start(self, local_url):
        self.base_url = self.base_url or local_url
        if self.enabled and not self.error and not self.worker:
            self.worker = threading.Thread(target=self._run, name='feishu-notifications', daemon=True)
            self.worker.start()

    def record(self, case_id, job=None):
        if not self.enabled or self.error:
            return
        try:
            event = {'case_id':case_id, 'status':'failed', 'manual':0, 'event_count':0,
                     'reports':0, 'paused':bool((job or {}).get('provider_paused'))}
            identity = {'job_id':(job or {}).get('job_id'), 'input':(job or {}).get('input_fingerprint')}
            try:
                directory = self.resolve_case(case_id)
                case = self.reviews.get_case(directory)
                task = review_task(case)
                identity['input'] = case['input_fingerprint']
                event['manual'] = task['counts']['needs_human']
                for candidate in case['candidates']:
                    disposition = candidate['disposition']
                    confirmed = disposition['status'] == 'ai_confirmed_event' or (
                        disposition['status'] == 'human_reviewed' and disposition.get('human_decision') == 'confirmed_event')
                    if confirmed:
                        event['event_count'] += len(disposition.get('confirmed_intervals', []))
                        event['reports'] += self.reviews.get_investigation(directory, candidate['candidate_id']).get('status') == 'model_validated'
                state = (job or {}).get('status', 'complete')
                event['status'] = (state if state in {'failed','changed'} else
                    'data_issue' if not task['data_quality_confirmed'] else
                    'manual' if event['manual'] else 'failed' if task['counts']['pending_ai'] else 'done')
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Only a generic failure state goes to the group, never raw exceptions.
            payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
            key = hashlib.sha256((json.dumps(identity, sort_keys=True)+payload).encode()).hexdigest()
            with self._db() as db:
                if db.execute('SELECT 1 FROM notices WHERE id=?', (key,)).fetchone():
                    return
                db.execute("UPDATE notices SET state='superseded' WHERE case_id=? AND state IN ('pending','failed','unknown')", (case_id,))
                db.execute('INSERT INTO notices VALUES (?,?,?,?,?,NULL)', (key,case_id,payload,'pending',time.time()))
            self.wake.set()
        except (OSError, sqlite3.Error):
            self.error = '通知记录保存失败，分析流程继续；检查磁盘后重启服务'

    def status(self):
        result = {'enabled':bool(self.enabled), 'error':self.error, 'counts':dict.fromkeys(STATES, 0),
                  'last_warning':None}
        if self.enabled and not self.error:
            try:
                with self._db() as db:
                    result['counts'].update(dict(db.execute('SELECT state, COUNT(*) FROM notices GROUP BY state')))
                    warning = db.execute("SELECT warning FROM notices WHERE state IN ('failed','unknown') ORDER BY created DESC LIMIT 1").fetchone()
                result['last_warning'] = warning[0] if warning else None
            except sqlite3.Error:
                result['error'] = '通知记录无法读取'
        return result

    def retry(self):
        if not self.enabled or self.error:
            raise ValueError('飞书未启用或通知存储异常，请检查配置后重启')
        with self._db() as db:
            db.execute("UPDATE notices SET state='pending', created=?, warning=NULL WHERE state IN ('failed','unknown')", (time.time(),))
        self.wake.set()
        return self.status()

    def _deliver(self):
        with self._db() as db:
            rows = db.execute("SELECT id,payload,created FROM notices WHERE state='pending' ORDER BY created LIMIT 20").fetchall()
            if not rows or time.time() - rows[0][2] < self.config['summary_window_seconds']:
                return
            db.executemany("UPDATE notices SET state='sending' WHERE id=?", [(row[0],) for row in rows])
        try:
            card = progress_card([json.loads(row[1]) for row in rows], self.base_url)
            warning = self.sender(self.config['webhook_url'], *card,
                                  signing_secret=self.config['signing_secret'], keyword=self.config['keyword'])
        except Exception as error:
            warning = '通知发送异常：' + type(error).__name__
        # Persist only the transport's sanitized diagnostic, never payloads or secrets in errors.
        with self._db() as db:
            for row in rows:
                newer = db.execute('SELECT 1 FROM notices a JOIN notices b ON a.case_id=b.case_id '
                                   'WHERE a.id=? AND b.rowid>a.rowid LIMIT 1', (row[0],)).fetchone()
                state = 'superseded' if warning and newer else 'failed' if warning else 'sent'
                db.execute('UPDATE notices SET state=?, warning=? WHERE id=?', (state,warning,row[0]))

    def _run(self):
        while not self.closed:
            self.wake.wait(.5)
            self.wake.clear()
            if self.closed:
                return
            try:
                self._deliver()
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                self.error = '通知存储异常，已停止发送；分析流程继续'
                return

    def close(self):
        self.closed = True
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=7)


def main(argv=None):
    """Explicit test is the only CLI action here that sends to a real group."""
    import argparse
    parser = argparse.ArgumentParser(description='飞书通知配置检查')
    parser.add_argument('--test', action='store_true', help='向已配置的群发送一条测试通知')
    args = parser.parse_args(argv)
    config = load_config()
    if config['error'] or not config['enabled']:
        print(config['error'] or '飞书未启用：填写 configs/feishu.local.json 并将 enabled 改为 true')
        return 1
    if not args.test:
        print('飞书配置有效；签名校验：' + ('已配置' if config['signing_secret'] else '未配置') + '。尚未发送消息。')
        return 0
    warning = send_card(config['webhook_url'], 'case-analyzer · 连接测试', ['工作台飞书通知连接正常。'],
                        signing_secret=config['signing_secret'], keyword=config['keyword'])
    print(warning or '发送成功，请检查飞书群。')
    return 1 if warning else 0

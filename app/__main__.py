"""Start the portable local application: python -m app."""
import argparse
import json
import os
import threading
import webbrowser
from pathlib import Path
from app.paths import DEFAULT_RUNTIME
from app.server import WorkbenchContext, create_server
from app.workspace import initialize, load_workspace, check_workspace, model_status


class WorkspaceLease:
    """One writer service per runtime; OS releases the lease after a crash."""
    def __init__(self, runtime):
        self.file = (runtime / 'workspace.lock').open('a+b')
        try:
            if self.file.seek(0, os.SEEK_END) == 0:
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError('这个工作空间已由另一服务打开，请使用已有页面或停止原服务') from None

    def close(self):
        self.file.close()


def make_context(runtime, config):
    ready = model_status()['ready']
    evaluation=(runtime/'evaluations/benchmark.json').is_file()
    return WorkbenchContext(batch_dir=runtime/'index', data_root=runtime/'data',
        annotations_root=runtime/'annotations', evaluation_output=runtime/'evaluations',
        model_reviews_root=runtime/'model_reviews', automatic_review=config['automatic_review'] and ready and not evaluation,
        workspace_id='local', workspace_name='独立效果评估' if evaluation else config['name'])


def main():
    parser = argparse.ArgumentParser(description='Case Analyzer 独立工作台')
    parser.add_argument('--runtime', type=Path, default=DEFAULT_RUNTIME, help='运行数据目录，默认本应用的 runtime/')
    parser.add_argument('--port', type=int)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--no-auto-review', action='store_true')
    parser.add_argument('--check', action='store_true', help='初始化空目录并检查应用资源，不启动服务或请求模型')
    parser.add_argument('--check-api', action='store_true', help='仅发送一次短请求检查 API，不载入数据或启动复核队列')
    args = parser.parse_args()
    lease = server = None
    try:
        if args.check_api:
            from app.api_check import check_api
            result = check_api()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['ok'] else 1
        config = load_workspace()
        if args.no_auto_review:
            config['automatic_review'] = False
        port = args.port if args.port is not None else config['port']
        if not 0 <= port <= 65535:
            raise ValueError('端口必须在 0～65535 之间')
        runtime = initialize(args.runtime)
        context = make_context(runtime, config)
        if args.check:
            result = check_workspace(context)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['ok'] else 1
        lease = WorkspaceLease(runtime)
        server = create_server(context, port)
        url = f'http://127.0.0.1:{server.server_port}/'
        print(f'Case Analyzer 已启动：{url}\n{model_status()["message"]}\n按 Ctrl+C 停止。', flush=True)
        if not args.no_open:
            threading.Timer(.5, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n工作台已停止。')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    finally:
        if server:
            server.server_close()
        if lease:
            lease.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

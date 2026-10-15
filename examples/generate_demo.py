"""Generate deterministic illustrative CSVs without external data or models."""
import argparse
import csv
import io
import json
from pathlib import Path


def table(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


def demo_cases():
    cases = {}
    for case_id, multiple, decelerating in (
        ('demo_deceleration', False, True),
        ('demo_multitarget', True, True),
        ('demo_no_event', False, False),
    ):
        vehicle, objects, planning, control = [], [], [], []
        distance = 45.0
        for i in range(61):
            t = i/10
            speed = 20-3*max(0,min(t-1.5,3)) if decelerating else 20.0
            acceleration = -3.0 if decelerating and 15<i<=45 else 0.0
            braking = acceleration < 0
            vehicle.append([f'{t:.1f}',f'{speed:.3f}',f'{acceleration:.3f}',.6 if braking else 0])
            relative = 16-speed
            if i: distance += relative*.1
            objects.append([f'{t:.1f}','target_a',f'{distance:.3f}',f'{relative:.3f}',.95])
            if multiple:
                objects.append([f'{t:.1f}','target_b',f'{distance+12:.3f}',f'{relative+1:.3f}',.9])
            planning.append([f'{t:.1f}',f'{max(0,speed+acceleration*.1):.3f}',acceleration,
                             'FOLLOW_LEAD_VEHICLE' if braking else 'CRUISE'])
            control.append([f'{t:.1f}',.6 if braking else 0,.15 if not braking else 0,acceleration])
        files = {'vehicle_state.csv':table(['timestamp','speed_mps','acceleration_mps2','brake'],vehicle)}
        if decelerating:
            files.update({
                'object_state.csv':table(['timestamp','object_id','distance_m','relative_speed_mps','confidence'],objects),
                'planning_state.csv':table(['timestamp','target_speed_mps','planned_acceleration_mps2','decision_reason'],planning),
                'control_state.csv':table(['timestamp','brake_command','throttle_command','requested_acceleration_mps2'],control),
            })
        cases[case_id] = {'case_id':case_id, 'source_type':'synthetic',
            'source_name':'公式生成的演示日志；不是道路采集或闭环仿真',
            'acceleration_source':'由演示车速有限差分得到，不是独立传感器测量', 'files':files}
    return cases


def generate(output, force=False):
    for cid, payload in demo_cases().items():
        directory = output/cid
        content = dict(payload['files'])
        content['source.json'] = json.dumps({k:v for k,v in payload.items() if k!='files'},ensure_ascii=False,indent=2)+'\n'
        if not force and any((directory/name).exists() for name in content):
            raise FileExistsError(f'{directory} 已有样例；需要覆盖时使用 --force')
        directory.mkdir(parents=True, exist_ok=True)
        for name,text in content.items():
            (directory/name).write_text(text,encoding='utf-8',newline='\n')
    print(f'Generated {len(demo_cases())} synthetic cases: {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--force',action='store_true')
    args = parser.parse_args(); generate(args.output,args.force)

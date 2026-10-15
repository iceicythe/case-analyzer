"""Read vehicle logs without silently sorting or filling missing measurements."""
import numpy as np
import pandas as pd


def load_dataframe(path):
    frame = pd.read_csv(path)
    if frame.empty or not {'timestamp', 'speed_mps'} <= set(frame):
        raise ValueError('车辆日志需要 timestamp、speed_mps 及至少一行数据')
    for name in ('timestamp', 'speed_mps'):
        frame[name] = pd.to_numeric(frame[name], errors='raise')
        if not np.isfinite(frame[name]).all():
            raise ValueError(name + ' 含无效数值')
    frame['vehicle_source_row'] = range(2, len(frame) + 2)
    return frame

# CSV 输入约定

支持批量目录或单个 Case 导入。批量时按 CSV 所在目录分组，编号默认取末级目录名；具体操作见 [接入流程](WORKFLOW.md)。每条 Case 支持以下四个固定文件名。UTF-8 编码、第一行为唯一字段名，所有文件的 `timestamp` 使用相同时间基准，单位秒。

| 文件 | 字段与单位 |
| --- | --- |
| `vehicle_state.csv` | 必需 `timestamp`、`speed_mps`（m/s）；可选 `acceleration_mps2`（m/s²）、`brake`（0～1） |
| `object_state.csv` | `timestamp`、`object_id`、`distance_m`（m）、`relative_speed_mps`（m/s，负数表示接近）、`confidence`（0～1） |
| `planning_state.csv` | `timestamp`、`target_speed_mps`（m/s）、`planned_acceleration_mps2`（m/s²）、`decision_reason` |
| `control_state.csv` | `timestamp`、`brake_command`（0～1）、`throttle_command`（0～1）、`requested_acceleration_mps2`（m/s²） |

车辆文件必需，其他文件可缺省。可选模块只含表头、缺少部分信号或未覆盖事件时，排查中会说明证据不足。额外列保留在原始导入文件，但当前排查仅使用上述已支持字段。

车速须为有限非负数；车辆时间严格递增、按 0.1 秒采样。每份 CSV 最多 200000 行，单个 Case 文件合计最多 24 MB。车辆至少需要 6 个点。时间乱序、重复车辆时间和无效车速拒绝导入；采样缺口允许导入查看，阻止自动确认。目标日志允许同一时间有不同目标记录。

当前事件口径是项目内的 SD-WINDOW-1.1：0.5 秒内车速下降至少 1.0 m/s，5 个间隔中至少 4 个下降不小于 0.01 m/s，累计回升不超过 0.1 m/s。重叠窗口合并；不相连窗口只有在间隔不超过 0.5 秒、中间采样完整且没有回升、端点仍下降至少 0.01 m/s 时才连接。比较容差为 1e-6。宽松候选门槛另由 `candidate_screening.json` 定义。

导入时明确来源为采集日志、合成数据或来源待核查。来源待核查的 Case 不能直接确认事件。加速度来源未声明时保留未知；由车速求导得到的信号仅作辅助证据。TTC 根据目标距离和接近速度计算，仅在条件满足时提供参考。

系统保存车辆文件及元数据的导入哈希，用于发现后续修改；这不等于对真实采集过程的认证。原始 CAN、厂商私有日志、不同频率与不同单位需要先转换，转换过程和采样缺口应如实保留。旧项目目录里的数据不会被自动读入或当作真实新样本。

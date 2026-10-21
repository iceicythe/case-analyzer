# sim-batch-02 场景合成数据评测

来源：脚本按预设场景生成；参考标签：程序区间经人工复核，非独立盲标。当前结果不是实车或闭环控制效果证明。

30条全部保留；27条计算事件指标；seg_022/023/024按预先约定独立评价质量处理，原人工答案未改动。

| 指标 | 结果 | 分母与解释 |
| --- | --- | --- |
| 候选召回率 | 100.00% | 27/27 个参考事件 |
| AI自动确认精确率 | 100.00% | TP=26，FP=0 |
| AI自动确认召回率 | 96.30% | TP=26，FN=1；转人工尚未确认的事件也计入未自动检出 |
| 自动处理覆盖率 | 86.67% | 26/30 个候选；分母含质量问题Case候选 |
| 数据问题处理符合率 | 100.00% | 3条缺口Case，检测质量问题且无自动结论/无模型调用 |
| 报告有用率 | 待人工评价 | 0/25 份；未自动填写评价 |
| AI排查输出格式/证据校验 | 23/25 | 失败2份，保留本地报告；此项不等于建议正确率 |

边界误差仅对成功匹配事件计算：{"start_error_seconds": 0.0, "end_error_seconds": 0.0, "matched_events": 26}

程序观测与设计预期：{"matched": 26, "assessed": 26, "not_assessed": 0, "rate": 1.0, "meaning": "程序观测分类与仿真设计预期的一致性；不是AI建议有用率或根因准确率"}

真实模型调用记录 51 条。事件匹配 IoU≥0.5；候选覆盖≥90%；错误拒绝覆盖≥50%。

## 逐Case对账

| Case | TP | FP | FN | 候选状态 |
| --- | --- | --- | --- | --- |
| seg_001 | 1 | 0 | 0 | ai_confirmed_event |
| seg_002 | 1 | 0 | 0 | ai_confirmed_event |
| seg_003 | 1 | 0 | 0 | ai_confirmed_event |
| seg_004 | 1 | 0 | 0 | ai_confirmed_event |
| seg_005 | 1 | 0 | 0 | ai_confirmed_event |
| seg_006 | 0 | 0 | 0 | 无候选 |
| seg_007 | 0 | 0 | 0 | 无候选 |
| seg_008 | 2 | 0 | 0 | ai_confirmed_event |
| seg_009 | 2 | 0 | 0 | ai_confirmed_event, ai_confirmed_event |
| seg_010 | 1 | 0 | 0 | ai_confirmed_event |
| seg_011 | 0 | 0 | 0 | ai_rejected_candidate |
| seg_012 | 3 | 0 | 0 | ai_confirmed_event, ai_confirmed_event, ai_confirmed_event |
| seg_013 | 1 | 0 | 0 | ai_confirmed_event |
| seg_014 | 1 | 0 | 0 | ai_confirmed_event |
| seg_015 | 1 | 0 | 0 | ai_confirmed_event |
| seg_016 | 1 | 0 | 0 | ai_confirmed_event |
| seg_017 | 0 | 0 | 0 | 无候选 |
| seg_018 | 1 | 0 | 0 | ai_confirmed_event |
| seg_019 | 1 | 0 | 0 | ai_confirmed_event |
| seg_020 | 1 | 0 | 0 | ai_confirmed_event |
| seg_021 | 1 | 0 | 0 | ai_confirmed_event |
| seg_022 | 排除 | — | — | needs_human |
| seg_023 | 排除 | — | — | needs_human, needs_human |
| seg_024 | 排除 | — | — | 无候选 |
| seg_025 | 1 | 0 | 0 | ai_confirmed_event |
| seg_026 | 1 | 0 | 0 | ai_confirmed_event |
| seg_027 | 1 | 0 | 0 | ai_confirmed_event |
| seg_028 | 1 | 0 | 0 | ai_confirmed_event |
| seg_029 | 1 | 0 | 0 | ai_confirmed_event |
| seg_030 | 0 | 0 | 1 | needs_human |

## 排查报告

- [seg_001_C001](report_records/seg_001_C001.md)：model_validated
- [seg_002_C001](report_records/seg_002_C001.md)：model_validated
- [seg_003_C001](report_records/seg_003_C001.md)：model_validated
- [seg_004_C001](report_records/seg_004_C001.md)：model_validated
- [seg_005_C001](report_records/seg_005_C001.md)：model_validated
- [seg_008_C001](report_records/seg_008_C001.md)：model_validated
- [seg_009_C001](report_records/seg_009_C001.md)：model_validated
- [seg_009_C002](report_records/seg_009_C002.md)：model_validated
- [seg_010_C001](report_records/seg_010_C001.md)：model_validated
- [seg_012_C001](report_records/seg_012_C001.md)：model_validated
- [seg_012_C002](report_records/seg_012_C002.md)：model_validated
- [seg_012_C003](report_records/seg_012_C003.md)：model_validated
- [seg_013_C001](report_records/seg_013_C001.md)：model_validated
- [seg_014_C001](report_records/seg_014_C001.md)：model_validated
- [seg_015_C001](report_records/seg_015_C001.md)：model_validated
- [seg_016_C001](report_records/seg_016_C001.md)：model_validated
- [seg_018_C001](report_records/seg_018_C001.md)：model_validated
- [seg_019_C001](report_records/seg_019_C001.md)：model_validated
- [seg_020_C001](report_records/seg_020_C001.md)：model_validated
- [seg_021_C001](report_records/seg_021_C001.md)：model_validated
- [seg_025_C001](report_records/seg_025_C001.md)：model_validated
- [seg_026_C001](report_records/seg_026_C001.md)：model_validated
- [seg_027_C001](report_records/seg_027_C001.md)：model_validated
- [seg_028_C001](report_records/seg_028_C001.md)：model_failed
- [seg_029_C001](report_records/seg_029_C001.md)：model_failed

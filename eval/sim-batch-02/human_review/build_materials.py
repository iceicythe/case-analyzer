"""Extract frozen review materials. --check verifies without writing ratings or outputs."""
import argparse
import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET = HERE.parent
SELECTED = ["seg_001_C001", "seg_008_C001", "seg_009_C001", "seg_009_C002",
            "seg_012_C001", "seg_016_C001", "seg_019_C001", "seg_020_C001",
            "seg_021_C001", "seg_025_C001", "seg_027_C001", "seg_028_C001"]
CHECK_SECTION = re.compile(r"### 下一步检查[^\n]*\n(.*?)(?=\n## |\Z)", re.S)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def materials():
    source = DATASET / "results" / "baseline_report_comparison.json"
    pairs = {f"{item['case_id']}_{item['candidate_id']}": item
             for item in read_json(source)["items"]}
    outputs, manifest = {}, []
    for rid in SELECTED:
        record_path = DATASET / "results" / "report_records" / f"{rid}.json"
        record = read_json(record_path)
        actual = record["report"]
        pair = pairs.get(rid)
        model_ok = record["status"] == "model_validated"
        kind = "paired_ai" if pair and model_ok else "fallback_control" if pair else "quality_only"
        # Never reconstruct missing historical baselines with current rules.
        if pair:
            if pair["actual_report"] != actual or pair["status"] != record["status"]:
                raise ValueError(f"{rid}: frozen comparison and report differ")
            baseline = pair["baseline_report"]
            if CHECK_SECTION.sub("", baseline) != CHECK_SECTION.sub("", actual):
                raise ValueError(f"{rid}: comparison paths do not share the same evidence")
            outputs[f"baselines/{rid}.md"] = baseline
        title = "AI 交付报告" if model_ok else "模型失败后的本地回退报告（不是 AI 成功报告）"
        lines = [f"# {rid}：评价清单", "",
                 f"状态：`{record['status']}`；评价范围：`{kind}`。", "",
                 "本文件供材料核对与评分使用。计时前按 scoresheet 指定顺序，分别打开单一路径的完整报告，",
                 "不要提前阅读本清单中的另一条路径。评价规则见 [说明](../README.md)。", "",
                 f"## A. {title}", "",
                 f"[完整交付报告](../../results/report_records/{rid}.md)", ""]
        for event, section in enumerate(CHECK_SECTION.findall(actual), 1):
            lines.extend([f"事件段 {event}：", "", section.strip(), ""])
        if pair:
            lines.extend(["## B. 冻结的实际本地规则建议", "",
                          f"[完整规则报告](../baselines/{rid}.md)", "",
                          "从 baseline_report_comparison.json 原文提取；与 A 的事实和限制段逐字相同。", ""])
            for event, section in enumerate(CHECK_SECTION.findall(baseline), 1):
                lines.extend([f"事件段 {event}：", "", section.strip(), ""])
        else:
            lines.extend(["## B. 无冻结规则对照", "",
                          "本报告只评价证据、可执行性和无依据结论；不填规则耗时、AI 耗时比或配对优劣。",
                          "如要比较当前版本，需另建批次冻结同一版本的规则与 AI 输出。", ""])
        outputs[f"checklists/{rid}.md"] = "\n".join(lines)
        manifest.append({"report_id": rid, "status": record["status"], "scope": kind,
                         "report_record_sha256": sha(record_path),
                         "baseline_source": "results/baseline_report_comparison.json" if pair else None,
                         "baseline_source_sha256": sha(source) if pair else None})
    outputs["material_manifest.json"] = json.dumps(
        {"version": 2, "note": "冻结材料提取；不生成模型结果，不填写真人评分。",
         "reports": manifest}, ensure_ascii=False, indent=2) + "\n"
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs = materials()
    failures = []
    for relative, text in outputs.items():
        path = HERE / relative
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                failures.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    if failures:
        raise SystemExit("Review materials differ: " + ", ".join(failures))
    print(f"{'Verified' if args.check else 'Generated'} {len(outputs)} files; ratings untouched")


if __name__ == "__main__":
    main()

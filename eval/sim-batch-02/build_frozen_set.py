# -*- coding: utf-8 -*-
"""组装 sim-batch-02 脱敏冻结评测集。

用法：
    python build_frozen_set.py --sim <sim_dataset 根> --run <acceptance run 根> --out <发布包目录>

产物布局（相对 --out）：
    README.md / build_frozen_set.py / verify.py / verify_report.txt / file_hashes.csv / SHA256SUMS
    generate.py / case_manifest.csv / signal_dictionary.csv / campaign.json（脱敏）
    _human_review_2026-10-15.json      人工复核记录（放包根，避开 verify.py 对 reference_labels 的清理）
    _expected/                         设计意图（verify.py 读取）
    cases/                             工具实际消费的输入（run/data 原样拷贝）
    reference_labels/                  人工复核生效标签（human_verified）
    results/                           首次自动评测冻结结果 + 纯规则基线 + 已交付报告
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

RESULT_FILES = [
    "result.json",
    "predictions.json",
    "metrics.md",
    "baseline.json",
    "baseline_predictions.json",
    "scoring_references.json",
    "input_quality.json",
    "baseline_report_comparison.json",
    "frozen_manifest.json",
]

# campaign.json 中需要脱敏的字段：本地绝对路径 → 相对说明
REDACTED_SOURCE_DIR = "sim_dataset/workbench_import（发布时将本地绝对路径脱敏；输入内容与本包 cases/ 一致，逐文件哈希见 input_hashes）"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in sorted(src.rglob("*")):
        if p.is_file():
            copy_file(p, dst / p.relative_to(src))
            n += 1
    return n


def desensitize_campaign(src: Path, dst: Path) -> None:
    data = json.loads(src.read_text(encoding="utf-8"))
    if "source_directory" in data:
        data["source_directory"] = REDACTED_SOURCE_DIR
        data["source_directory_redacted"] = True
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(sim: Path, run: Path, out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    counts = {}

    # 顶层文件：数据集自带材料
    for name in ("verify.py", "verify_report.txt", "file_hashes.csv",
                 "generate.py", "case_manifest.csv", "signal_dictionary.csv"):
        copy_file(sim / name, out / name)
        counts[name] = 1

    # 人工复核记录：放包根（reference_labels/ 内的 json 会被 verify.py 清理重写）
    copy_file(sim / "reference_labels" / "_human_review_2026-10-15.json",
              out / "_human_review_2026-10-15.json")
    counts["_human_review_2026-10-15.json"] = 1

    # 目录：设计意图 / 输入 / 人工生效标签
    counts["_expected/"] = copy_tree(sim / "_expected", out / "_expected")
    counts["cases/"] = copy_tree(run / "data", out / "cases")
    counts["reference_labels/"] = copy_tree(sim / "reference_labels", out / "reference_labels")
    # reference_labels 里只保留 30 个 seg_*.json（复核记录已在包根）
    for p in (out / "reference_labels").glob("*.json"):
        if not p.name.startswith("seg_"):
            p.unlink()

    # 结果：首次自动评测 + 基线 + 质量门控 + 对照 + 指纹 + 已交付报告
    results = out / "results"
    results.mkdir()
    for name in RESULT_FILES:
        copy_file(run / name, results / name)
        counts[f"results/{name}"] = 1
    counts["results/report_records/"] = copy_tree(run / "report_records", results / "report_records")

    # campaign.json 脱敏
    desensitize_campaign(run / "campaign.json", out / "campaign.json")
    counts["campaign.json"] = 1

    # SHA256SUMS（覆盖本包全部文件，含之后拷入的 build_frozen_set.py 之前生成，故最后单独重算）
    write_sums(out)
    for k, v in counts.items():
        print(f"  {k:<40} {v} files")
    total = sum(1 for _ in out.rglob("*") if _.is_file())
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"TOTAL {total} files, {size / 1e6:.2f} MB -> {out}")


def write_sums(out: Path) -> None:
    lines = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            rel = p.relative_to(out).as_posix()
            lines.append(f"{sha256(p)}  {rel}")
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", required=True, type=Path)
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    build(args.sim.resolve(), args.run.resolve(), args.out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())

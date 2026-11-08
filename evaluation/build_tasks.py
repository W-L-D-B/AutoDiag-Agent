"""Build the fixed task set before any held-out model execution."""

import hashlib
import json
from pathlib import Path

FAMILIES = (
    (
        "normal",
        "baseline",
        "检查 BMS、HVAC 和 Body 的当前状态，判断是否存在可证实的异常。",
        ["healthy"],
        "none",
    ),
    (
        "data_loss",
        "drop_bms_data",
        "BMS 页面读数停止更新。请比较心跳、数据新鲜度和诊断通路，定位问题层次。",
        ["data_stale"],
        "none",
    ),
    (
        "crc",
        "corrupt_hvac_crc",
        "HVAC 的数据显示异常。请检查传输有效性和拒绝计数，给出证据。",
        ["bad_crc"],
        "none",
    ),
    (
        "frozen_counter",
        "body_sequence_freeze",
        "Body 的数据为什么没有有效更新？请区分进程存活与有效状态帧。",
        ["duplicate_counter"],
        "none",
    ),
    (
        "temperature",
        "bms_overtemperature",
        "检查 BMS 温度异常，区分有效高温测量与通信错误。",
        ["overtemperature"],
        "none",
    ),
    (
        "lin_input",
        "lin_bad_checksum",
        "检查 HVAC 的温度输入是否可信，区分 CAN 传输与传感器输入质量。",
        ["lin_input_invalid"],
        "none",
    ),
    (
        "node_exit",
        "node_exit",
        "Body 的车身数据不更新，请判断该 ECU 当前是否在线并说明依据。",
        ["ecu_offline"],
        "none",
    ),
    (
        "old_sequence",
        "old_bms_sequence",
        "BMS 持续有报文到达，但业务数据没有更新。检查旧序号和新鲜度。",
        ["old_sequence"],
        "none",
    ),
    (
        "scale",
        "wrong_bms_scale",
        "BMS 页面电压明显不合理。请比较周期 CAN 电压与 D100 诊断值，检查缩放是否一致。",
        ["scaling_mismatch"],
        "none",
    ),
    (
        "service",
        "baseline",
        "现在无法取得车辆状态。请先判断采集服务是否可用，不要把服务故障当成 ECU 掉线。",
        ["service_unavailable", "insufficient_evidence"],
        "stop_service",
    ),
    (
        "history",
        "baseline",
        "请给出昨天凌晨两点 BMS 的准确温度，必须有当时的采样证据；没有就明确说明数据不足。",
        ["insufficient_evidence"],
        "none",
    ),
    (
        "unsupported_did",
        "baseline",
        "读取 BMS 的 DID 0x9999。如果 ECU 不支持该 DID，请明确说明诊断不可用，不要编造数值。",
        ["diagnostic_unavailable", "insufficient_evidence"],
        "none",
    ),
)


def build_tasks():
    tasks = []
    counters = (0, 64, 127, 250, 255)
    for family, scenario, question, expected, preparation in FAMILIES:
        for variant, counter in enumerate(counters):
            tasks.append(
                {
                    "id": f"task-{len(tasks) + 1:03d}",
                    "family": family,
                    "split": "development" if variant < 3 else "holdout",
                    "scenario": scenario,
                    "seed": variant + 1,
                    "counter_start": counter,
                    "question": question,
                    "expected_codes": expected,
                    "preparation": preparation,
                }
            )
    return {
        "schema_version": 1,
        "description": "12 task families x 5 real seed/counter variants; 36 development, 24 held-out. Truth is evaluator-only.",
        "tasks": tasks,
    }


def main():
    path = Path(__file__).with_name("tasks.json")
    content = json.dumps(build_tasks(), ensure_ascii=False, indent=2) + "\n"
    path.write_text(content, encoding="utf-8")
    print(
        "Tasks frozen:",
        len(build_tasks()["tasks"]),
        "sha256",
        hashlib.sha256(content.encode()).hexdigest(),
    )


if __name__ == "__main__":
    main()

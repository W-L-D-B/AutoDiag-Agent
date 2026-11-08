"""Rule baseline using the same question and observable RPC data, never task truth."""

from autodiag_agent.rpc import RpcError


async def diagnose(client, question: str) -> str:
    try:
        snapshot = await client.call("snapshot", {})
    except (RpcError, OSError, TimeoutError):
        return "service_unavailable"
    if "昨天" in question or "凌晨两点" in question:
        return "insufficient_evidence"
    if "0x9999" in question.lower():
        try:
            await client.call("read_did", {"node": "bms", "did": 0x9999})
        except (RpcError, OSError, TimeoutError):
            return "diagnostic_unavailable"
        return "healthy"
    nodes = snapshot["nodes"]
    target = (
        "hvac"
        if "HVAC" in question or "空调" in question
        else "body"
        if "Body" in question or "车身" in question
        else "bms"
    )
    state = nodes[target]
    if not state["online"]:
        return "ecu_offline"
    if state["data_status"] != "fresh":
        counts = state["rejections"]
        if counts["bad_crc"]:
            return "bad_crc"
        if counts["old"]:
            return "old_sequence"
        if counts["duplicate"]:
            return "duplicate_counter"
        return "data_stale"
    signals = state["signals"] or {}
    if target == "hvac" and not signals.get("cabin_sensor_valid", False):
        return "lin_input_invalid"
    if target == "bms" and signals.get("temperature_c", 0) > 90:
        return "overtemperature"
    if target == "bms" and signals.get("voltage_v", 400) < 100:
        try:
            actual = await client.call("read_did", {"node": "bms", "did": 0xD100})
            if abs(actual["values"]["voltage_v"] - signals["voltage_v"]) > 1:
                return "scaling_mismatch"
        except (RpcError, OSError, TimeoutError):
            return "diagnostic_unavailable"
    return "healthy"

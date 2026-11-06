#include "autodiag/state.hpp"

#include <nlohmann/json.hpp>

#include <array>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>

namespace autodiag {

namespace {

[[nodiscard]] constexpr std::uint64_t elapsed_since(const std::uint64_t now_ms,
                                                    const std::uint64_t then_ms) noexcept {
    return now_ms >= then_ms ? now_ms - then_ms : 0U;
}

void increment(RejectionCounts &counts, const RejectReason reason) noexcept {
    switch (reason) {
    case RejectReason::bad_crc:
        ++counts.bad_crc;
        break;
    case RejectReason::bad_length:
        ++counts.bad_length;
        break;
    case RejectReason::invalid_flags:
        ++counts.invalid_flags;
        break;
    case RejectReason::unknown_id:
        ++counts.unknown_id;
        break;
    case RejectReason::invalid_payload:
        ++counts.invalid_payload;
        break;
    case RejectReason::duplicate:
        ++counts.duplicate;
        break;
    case RejectReason::old:
        ++counts.old;
        break;
    case RejectReason::accepted:
    case RejectReason::resynchronized:
        break;
    }
}

[[nodiscard]] bool is_forward_counter(const std::uint8_t previous,
                                      const std::uint8_t next) noexcept {
    const std::uint8_t delta = static_cast<std::uint8_t>(next - previous);
    return delta >= 1U && delta <= 127U;
}

[[nodiscard]] RejectReason sequence_reason(const std::optional<std::uint8_t> previous,
                                           const std::uint8_t next) noexcept {
    if (!previous.has_value() || is_forward_counter(*previous, next)) {
        return RejectReason::accepted;
    }
    return *previous == next ? RejectReason::duplicate : RejectReason::old;
}

[[nodiscard]] SignalValues to_signals(const DecodedPayload &payload) {
    if (const auto *status = std::get_if<BmsStatus>(&payload); status != nullptr) {
        return BmsSignals{voltage_volts(*status), current_amps(*status),
                          temperature_celsius(*status)};
    }
    if (const auto *status = std::get_if<HvacStatus>(&payload); status != nullptr) {
        return HvacSignals{cabin_temperature_celsius(*status), target_temperature_celsius(*status),
                           status->fan_level, status->cabin_valid};
    }
    if (const auto *status = std::get_if<BodyStatus>(&payload); status != nullptr) {
        return BodySignals{speed_kph(*status), status->door_mask, status->light_mask,
                           supply_voltage_volts(*status)};
    }
    throw std::logic_error("data frame does not contain a data payload");
}

[[nodiscard]] nlohmann::json signals_to_json(const SignalValues &signals) {
    return std::visit(
        [](const auto &value) -> nlohmann::json {
            using Value = std::decay_t<decltype(value)>;
            if constexpr (std::is_same_v<Value, BmsSignals>) {
                return {{"voltage_v", value.voltage_v},
                        {"current_a", value.current_a},
                        {"temperature_c", value.temperature_c}};
            } else if constexpr (std::is_same_v<Value, HvacSignals>) {
                return {{"cabin_temperature_c", value.cabin_temperature_c},
                        {"target_temperature_c", value.target_temperature_c},
                        {"fan_level", value.fan_level},
                        {"cabin_sensor_valid", value.cabin_sensor_valid}};
            } else {
                return {{"speed_kph", value.speed_kph},
                        {"door_mask", value.door_mask},
                        {"light_mask", value.light_mask},
                        {"supply_voltage_v", value.supply_voltage_v}};
            }
        },
        signals);
}

template <typename Value>
[[nodiscard]] nlohmann::json optional_to_json(const std::optional<Value> &value) {
    return value.has_value() ? nlohmann::json(*value) : nlohmann::json(nullptr);
}

} // namespace

MonitorState::MonitorState() {
    nodes_.emplace(Node::bms, NodeState{});
    nodes_.emplace(Node::hvac, NodeState{});
    nodes_.emplace(Node::body, NodeState{});
}

FrameDecision MonitorState::ingest(const Frame &frame, const std::uint64_t now_ms) {
    const DecodeResult decoded = decode_frame(frame);
    FrameDecision decision{false, decoded.reason, decoded.message, decoded.node, decoded.counter};

    const auto reject = [&](const RejectReason reason) {
        decision.reason = reason;
        increment(global_rejections_, reason);
        if (decision.node.has_value() && reason != RejectReason::unknown_id) {
            increment(nodes_.at(*decision.node).rejections, reason);
        }
        return decision;
    };

    if (decoded.reason != RejectReason::accepted) {
        return reject(decoded.reason);
    }
    if (!decoded.node.has_value()) {
        return reject(RejectReason::unknown_id);
    }

    NodeState &state = nodes_.at(*decoded.node);
    if (const auto *heartbeat = std::get_if<Heartbeat>(&decoded.payload); heartbeat != nullptr) {
        const bool had_heartbeat = state.heartbeat_received_at_ms.has_value();
        const bool was_offline =
            had_heartbeat &&
            elapsed_since(now_ms, *state.heartbeat_received_at_ms) >= kHeartbeatOfflineAfterMs;
        const bool uptime_reversed = state.heartbeat_uptime_ms.has_value() &&
                                     heartbeat->uptime_ms < *state.heartbeat_uptime_ms;
        const bool epoch_changed = had_heartbeat && (was_offline || uptime_reversed);

        if (!epoch_changed) {
            const RejectReason sequence =
                sequence_reason(state.heartbeat_counter, heartbeat->counter);
            if (sequence != RejectReason::accepted) {
                return reject(sequence);
            }
        }

        state.heartbeat_counter = heartbeat->counter;
        state.heartbeat_received_at_ms = now_ms;
        state.heartbeat_uptime_ms = heartbeat->uptime_ms;
        decision.accepted = true;
        if (epoch_changed) {
            state.data_counter.reset();
            state.data_received_at_ms.reset();
            state.signals.reset();
            ++state.resynchronizations;
            decision.reason = RejectReason::resynchronized;
        }
        return decision;
    }

    if (!decoded.counter.has_value()) {
        return reject(RejectReason::invalid_payload);
    }
    const std::uint8_t counter = *decoded.counter;
    const RejectReason sequence = sequence_reason(state.data_counter, counter);
    if (sequence != RejectReason::accepted) {
        return reject(sequence);
    }

    state.data_counter = counter;
    state.data_received_at_ms = now_ms;
    state.signals = to_signals(decoded.payload);
    decision.accepted = true;
    decision.reason = RejectReason::accepted;
    return decision;
}

MonitorSnapshot MonitorState::snapshot(const std::uint64_t now_ms) const {
    MonitorSnapshot result{};
    result.elapsed_ms = now_ms;
    result.global_rejections = global_rejections_;

    for (const auto &[node, state] : nodes_) {
        NodeSnapshot output{};
        output.data_counter = state.data_counter;
        output.heartbeat_counter = state.heartbeat_counter;
        output.signals = state.signals;
        output.rejections = state.rejections;
        output.resynchronizations = state.resynchronizations;

        if (state.data_received_at_ms.has_value()) {
            output.data_age_ms = elapsed_since(now_ms, *state.data_received_at_ms);
            output.data_status =
                *output.data_age_ms < kDataStaleAfterMs ? DataStatus::fresh : DataStatus::stale;
        }
        if (state.heartbeat_received_at_ms.has_value()) {
            output.heartbeat_age_ms = elapsed_since(now_ms, *state.heartbeat_received_at_ms);
            output.online = *output.heartbeat_age_ms < kHeartbeatOfflineAfterMs;
        }
        result.nodes.emplace(node, output);
    }
    return result;
}

std::string_view data_status_name(const DataStatus status) noexcept {
    switch (status) {
    case DataStatus::missing:
        return "missing";
    case DataStatus::fresh:
        return "fresh";
    case DataStatus::stale:
        return "stale";
    }
    return "missing";
}

void to_json(nlohmann::json &output, const RejectionCounts &counts) {
    output = {{"bad_crc", counts.bad_crc},
              {"bad_length", counts.bad_length},
              {"invalid_flags", counts.invalid_flags},
              {"unknown_id", counts.unknown_id},
              {"invalid_payload", counts.invalid_payload},
              {"duplicate", counts.duplicate},
              {"old", counts.old}};
}

void to_json(nlohmann::json &output, const MonitorSnapshot &snapshot) {
    nlohmann::json nodes = nlohmann::json::object();
    for (const auto &[node, state] : snapshot.nodes) {
        nodes[std::string{node_name(node)}] = {
            {"online", state.online},
            {"data_status", std::string{data_status_name(state.data_status)}},
            {"data_age_ms", optional_to_json(state.data_age_ms)},
            {"heartbeat_age_ms", optional_to_json(state.heartbeat_age_ms)},
            {"data_counter", optional_to_json(state.data_counter)},
            {"heartbeat_counter", optional_to_json(state.heartbeat_counter)},
            {"signals",
             state.signals.has_value() ? signals_to_json(*state.signals) : nlohmann::json(nullptr)},
            {"rejections", state.rejections},
            {"resynchronizations", state.resynchronizations},
        };
    }
    output = {{"elapsed_ms", snapshot.elapsed_ms},
              {"global_rejections", snapshot.global_rejections},
              {"nodes", std::move(nodes)}};
}

} // namespace autodiag

#pragma once

#include "autodiag/protocol.hpp"

#include <cstdint>
#include <map>
#include <optional>
#include <string_view>
#include <variant>

#include <nlohmann/json_fwd.hpp>

namespace autodiag {

constexpr std::uint64_t kDataStaleAfterMs{500U};
constexpr std::uint64_t kHeartbeatOfflineAfterMs{1200U};

enum class DataStatus : std::uint8_t {
    missing,
    fresh,
    stale,
};

struct RejectionCounts {
    std::uint64_t bad_crc{};
    std::uint64_t bad_length{};
    std::uint64_t invalid_flags{};
    std::uint64_t unknown_id{};
    std::uint64_t invalid_payload{};
    std::uint64_t duplicate{};
    std::uint64_t old{};
};

struct BmsSignals {
    double voltage_v{};
    double current_a{};
    double temperature_c{};
};

struct HvacSignals {
    double cabin_temperature_c{};
    double target_temperature_c{};
    std::uint8_t fan_level{};
    bool cabin_sensor_valid{};
};

struct BodySignals {
    double speed_kph{};
    std::uint8_t door_mask{};
    std::uint8_t light_mask{};
    double supply_voltage_v{};
};

using SignalValues = std::variant<BmsSignals, HvacSignals, BodySignals>;

struct NodeSnapshot {
    bool online{};
    DataStatus data_status{DataStatus::missing};
    std::optional<std::uint64_t> data_age_ms{};
    std::optional<std::uint64_t> heartbeat_age_ms{};
    std::optional<std::uint8_t> data_counter{};
    std::optional<std::uint8_t> heartbeat_counter{};
    std::optional<SignalValues> signals{};
    RejectionCounts rejections{};
    std::uint64_t resynchronizations{};
};

struct MonitorSnapshot {
    std::uint64_t elapsed_ms{};
    RejectionCounts global_rejections{};
    std::map<Node, NodeSnapshot> nodes{};
};

struct FrameDecision {
    bool accepted{};
    RejectReason reason{RejectReason::unknown_id};
    MessageType message{MessageType::unknown};
    std::optional<Node> node{};
    std::optional<std::uint8_t> counter{};
};

class MonitorState {
  public:
    MonitorState();

    [[nodiscard]] FrameDecision ingest(const Frame &frame, std::uint64_t now_ms);
    [[nodiscard]] MonitorSnapshot snapshot(std::uint64_t now_ms) const;

  private:
    struct NodeState {
        std::optional<std::uint8_t> data_counter{};
        std::optional<std::uint8_t> heartbeat_counter{};
        std::optional<std::uint64_t> data_received_at_ms{};
        std::optional<std::uint64_t> heartbeat_received_at_ms{};
        std::optional<std::uint32_t> heartbeat_uptime_ms{};
        std::optional<SignalValues> signals{};
        RejectionCounts rejections{};
        std::uint64_t resynchronizations{};
    };

    std::map<Node, NodeState> nodes_;
    RejectionCounts global_rejections_{};
};

[[nodiscard]] std::string_view data_status_name(DataStatus status) noexcept;
void to_json(nlohmann::json &output, const RejectionCounts &counts);
void to_json(nlohmann::json &output, const MonitorSnapshot &snapshot);

} // namespace autodiag

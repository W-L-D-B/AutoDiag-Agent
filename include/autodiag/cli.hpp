#pragma once

#include "autodiag/protocol.hpp"

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

namespace autodiag {

enum class Fault : std::uint8_t {
    none,
    drop_data,
    bad_crc,
    freeze_counter,
    overtemperature,
    lin_bad_checksum,
    exit_process,
    old_sequence,
    wrong_scale,
};

struct EcuOptions {
    Node node{Node::bms};
    std::string interface_name{"vcan0"};
    std::uint64_t duration_ms{3000U};
    std::uint32_t seed{1U};
    std::uint8_t counter_start{};
    Fault fault{Fault::none};
    std::uint64_t fault_after_ms{1000U};
    std::uint64_t fault_duration_ms{1000U};
};

struct MonitorOptions {
    std::string interface_name{"vcan0"};
    std::uint64_t duration_ms{3000U};
    std::string snapshot_path{};
    std::string log_path{};
};

class UsageError final : public std::runtime_error {
  public:
    using std::runtime_error::runtime_error;
};

[[nodiscard]] EcuOptions parse_ecu_options(int argc, char *argv[]);
[[nodiscard]] MonitorOptions parse_monitor_options(int argc, char *argv[]);
[[nodiscard]] bool fault_is_active(const EcuOptions &options, std::uint64_t elapsed_ms) noexcept;
[[nodiscard]] std::string_view fault_name(Fault fault) noexcept;

} // namespace autodiag

#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <variant>

namespace autodiag {

constexpr std::uint8_t kClassicCanDlc{8U};
constexpr std::uint32_t kCanSffMask{0x000007FFU};
constexpr std::uint32_t kCanEffFlag{0x80000000U};
constexpr std::uint32_t kCanRtrFlag{0x40000000U};
constexpr std::uint32_t kCanErrFlag{0x20000000U};
constexpr std::uint32_t kCanFlagMask{kCanEffFlag | kCanRtrFlag | kCanErrFlag};

constexpr std::uint32_t kBmsStatusCanId{0x180U};
constexpr std::uint32_t kHvacStatusCanId{0x181U};
constexpr std::uint32_t kBodyStatusCanId{0x182U};
constexpr std::uint32_t kBmsHeartbeatCanId{0x700U};
constexpr std::uint32_t kHvacHeartbeatCanId{0x701U};
constexpr std::uint32_t kBodyHeartbeatCanId{0x702U};

enum class Node : std::uint8_t {
    bms = 1U,
    hvac = 2U,
    body = 3U,
};

enum class MessageType : std::uint8_t {
    unknown,
    bms_status,
    hvac_status,
    body_status,
    heartbeat,
};

enum class RejectReason : std::uint8_t {
    accepted,
    resynchronized,
    unknown_id,
    bad_length,
    bad_crc,
    invalid_flags,
    invalid_payload,
    duplicate,
    old,
};

struct Frame {
    std::uint32_t can_id{};
    std::uint8_t dlc{};
    std::array<std::uint8_t, kClassicCanDlc> data{};
};

struct BmsStatus {
    std::uint16_t voltage_deci_volts{};
    std::int16_t current_deci_amps{};
    std::int16_t temperature_deci_celsius{};
    std::uint8_t counter{};
};

struct HvacStatus {
    std::int16_t cabin_temperature_deci_celsius{};
    std::int16_t target_temperature_deci_celsius{};
    std::uint8_t fan_level{};
    bool cabin_valid{};
    std::uint8_t counter{};
};

struct BodyStatus {
    std::uint16_t speed_centikph{};
    std::uint8_t door_mask{};
    std::uint8_t light_mask{};
    std::uint16_t supply_centivolts{};
    std::uint8_t counter{};
};

struct Heartbeat {
    Node node{Node::bms};
    bool degraded_input{};
    std::uint32_t uptime_ms{};
    std::uint8_t counter{};
};

[[nodiscard]] bool operator==(const BmsStatus &left, const BmsStatus &right) noexcept;
[[nodiscard]] bool operator==(const HvacStatus &left, const HvacStatus &right) noexcept;
[[nodiscard]] bool operator==(const BodyStatus &left, const BodyStatus &right) noexcept;
[[nodiscard]] bool operator==(const Heartbeat &left, const Heartbeat &right) noexcept;

using DecodedPayload = std::variant<std::monostate, BmsStatus, HvacStatus, BodyStatus, Heartbeat>;

struct DecodeResult {
    MessageType message{MessageType::unknown};
    std::optional<Node> node{};
    std::optional<std::uint8_t> counter{};
    RejectReason reason{RejectReason::unknown_id};
    DecodedPayload payload{};
};

[[nodiscard]] constexpr std::uint32_t arbitration_id(const std::uint32_t can_id) noexcept {
    return can_id & kCanSffMask;
}

[[nodiscard]] constexpr bool has_nonstandard_flags(const std::uint32_t can_id) noexcept {
    return (can_id & kCanFlagMask) != 0U || (can_id & ~(kCanSffMask | kCanFlagMask)) != 0U;
}

[[nodiscard]] std::uint8_t app_crc8(std::uint32_t can_id,
                                    const std::array<std::uint8_t, kClassicCanDlc> &data) noexcept;
[[nodiscard]] bool has_valid_app_crc(const Frame &frame) noexcept;
[[nodiscard]] Frame encode_bms_status(const BmsStatus &status) noexcept;
[[nodiscard]] Frame encode_hvac_status(const HvacStatus &status) noexcept;
[[nodiscard]] Frame encode_body_status(const BodyStatus &status) noexcept;
[[nodiscard]] Frame encode_heartbeat(const Heartbeat &heartbeat) noexcept;
[[nodiscard]] DecodeResult decode_frame(const Frame &frame) noexcept;
[[nodiscard]] MessageType message_type_for_arbitration_id(std::uint32_t can_id) noexcept;
[[nodiscard]] std::optional<Node> node_for_message(MessageType message,
                                                   std::uint32_t can_id) noexcept;
[[nodiscard]] std::string_view message_name(MessageType message) noexcept;
[[nodiscard]] std::string_view node_name(Node node) noexcept;
[[nodiscard]] std::string_view reject_reason_name(RejectReason reason) noexcept;
[[nodiscard]] std::string data_to_upper_hex(const Frame &frame);

[[nodiscard]] double voltage_volts(const BmsStatus &status) noexcept;
[[nodiscard]] double current_amps(const BmsStatus &status) noexcept;
[[nodiscard]] double temperature_celsius(const BmsStatus &status) noexcept;
[[nodiscard]] double cabin_temperature_celsius(const HvacStatus &status) noexcept;
[[nodiscard]] double target_temperature_celsius(const HvacStatus &status) noexcept;
[[nodiscard]] double speed_kph(const BodyStatus &status) noexcept;
[[nodiscard]] double supply_voltage_volts(const BodyStatus &status) noexcept;

} // namespace autodiag

#include "autodiag/protocol.hpp"

#include <algorithm>
#include <array>
#include <cstddef>
#include <iomanip>
#include <limits>
#include <sstream>

namespace autodiag {
namespace {

constexpr std::uint8_t kCrcPolynomial{0x1DU};
constexpr std::uint8_t kCrcInitialValue{0xFFU};
constexpr std::uint8_t kCrcFinalXor{0xFFU};
constexpr std::uint8_t kHvacCabinValidBit{0x01U};
constexpr std::uint8_t kHeartbeatRunningBit{0x01U};
constexpr std::uint8_t kHeartbeatDegradedInputBit{0x04U};

constexpr std::uint16_t kBmsMaximumVoltageDeciVolts{8000U};
constexpr std::int16_t kBmsMinimumCurrentDeciAmps{-2000};
constexpr std::int16_t kBmsMaximumCurrentDeciAmps{2000};
constexpr std::int16_t kBmsMinimumTemperatureDeciCelsius{-400};
constexpr std::int16_t kBmsMaximumTemperatureDeciCelsius{1500};
constexpr std::int16_t kHvacMinimumCabinTemperatureDeciCelsius{-400};
constexpr std::int16_t kHvacMaximumCabinTemperatureDeciCelsius{1250};
constexpr std::int16_t kHvacMinimumTargetTemperatureDeciCelsius{160};
constexpr std::int16_t kHvacMaximumTargetTemperatureDeciCelsius{320};
constexpr std::uint8_t kHvacMaximumFanLevel{7U};
constexpr std::uint16_t kBodyMaximumSpeedCentikph{20000U};
constexpr std::uint8_t kBodyAllowedDoorMask{0x1FU};
constexpr std::uint8_t kBodyAllowedLightMask{0x07U};
constexpr std::uint16_t kBodyMaximumSupplyCentivolts{2500U};

[[nodiscard]] constexpr std::uint8_t crc_step(std::uint8_t crc, const std::uint8_t value) noexcept {
    crc ^= value;
    for (std::uint8_t bit{0U}; bit < 8U; ++bit) {
        crc = (crc & 0x80U) != 0U ? static_cast<std::uint8_t>((crc << 1U) ^ kCrcPolynomial)
                                  : static_cast<std::uint8_t>(crc << 1U);
    }
    return crc;
}

constexpr void write_u16_le(std::array<std::uint8_t, kClassicCanDlc> &data,
                            const std::size_t offset, const std::uint16_t value) noexcept {
    data.at(offset) = static_cast<std::uint8_t>(value & 0x00FFU);
    data.at(offset + 1U) = static_cast<std::uint8_t>((value >> 8U) & 0x00FFU);
}

constexpr void write_i16_le(std::array<std::uint8_t, kClassicCanDlc> &data,
                            const std::size_t offset, const std::int16_t value) noexcept {
    write_u16_le(data, offset, static_cast<std::uint16_t>(value));
}

constexpr void write_u32_le(std::array<std::uint8_t, kClassicCanDlc> &data,
                            const std::size_t offset, const std::uint32_t value) noexcept {
    for (std::size_t index{0U}; index < 4U; ++index) {
        data.at(offset + index) =
            static_cast<std::uint8_t>((value >> static_cast<std::uint32_t>(index * 8U)) & 0xFFU);
    }
}

[[nodiscard]] constexpr std::uint16_t
read_u16_le(const std::array<std::uint8_t, kClassicCanDlc> &data,
            const std::size_t offset) noexcept {
    return static_cast<std::uint16_t>(static_cast<std::uint16_t>(data.at(offset)) |
                                      (static_cast<std::uint16_t>(data.at(offset + 1U)) << 8U));
}

[[nodiscard]] constexpr std::int16_t
read_i16_le(const std::array<std::uint8_t, kClassicCanDlc> &data,
            const std::size_t offset) noexcept {
    return static_cast<std::int16_t>(read_u16_le(data, offset));
}

[[nodiscard]] constexpr std::uint32_t
read_u32_le(const std::array<std::uint8_t, kClassicCanDlc> &data,
            const std::size_t offset) noexcept {
    std::uint32_t value{};
    for (std::size_t index{0U}; index < 4U; ++index) {
        value |= static_cast<std::uint32_t>(data.at(offset + index))
                 << static_cast<std::uint32_t>(index * 8U);
    }
    return value;
}

[[nodiscard]] constexpr bool in_range(const std::int16_t value, const std::int16_t minimum,
                                      const std::int16_t maximum) noexcept {
    return value >= minimum && value <= maximum;
}

[[nodiscard]] Frame make_frame(const std::uint32_t can_id) noexcept {
    return Frame{can_id, kClassicCanDlc, {}};
}

void set_crc(Frame &frame) noexcept {
    frame.data.at(7U) = app_crc8(frame.can_id, frame.data);
}

[[nodiscard]] bool valid_bms(const BmsStatus &status) noexcept {
    return status.voltage_deci_volts <= kBmsMaximumVoltageDeciVolts &&
           in_range(status.current_deci_amps, kBmsMinimumCurrentDeciAmps,
                    kBmsMaximumCurrentDeciAmps) &&
           in_range(status.temperature_deci_celsius, kBmsMinimumTemperatureDeciCelsius,
                    kBmsMaximumTemperatureDeciCelsius);
}

[[nodiscard]] bool valid_hvac(const HvacStatus &status) noexcept {
    return in_range(status.cabin_temperature_deci_celsius, kHvacMinimumCabinTemperatureDeciCelsius,
                    kHvacMaximumCabinTemperatureDeciCelsius) &&
           in_range(status.target_temperature_deci_celsius,
                    kHvacMinimumTargetTemperatureDeciCelsius,
                    kHvacMaximumTargetTemperatureDeciCelsius) &&
           status.fan_level <= kHvacMaximumFanLevel;
}

[[nodiscard]] bool valid_body(const BodyStatus &status) noexcept {
    return status.speed_centikph <= kBodyMaximumSpeedCentikph &&
           (status.door_mask & ~kBodyAllowedDoorMask) == 0U &&
           (status.light_mask & ~kBodyAllowedLightMask) == 0U &&
           status.supply_centivolts <= kBodyMaximumSupplyCentivolts;
}

[[nodiscard]] std::optional<Node> node_for_heartbeat_id(const std::uint32_t can_id) noexcept {
    switch (arbitration_id(can_id)) {
    case kBmsHeartbeatCanId:
        return Node::bms;
    case kHvacHeartbeatCanId:
        return Node::hvac;
    case kBodyHeartbeatCanId:
        return Node::body;
    default:
        return std::nullopt;
    }
}

[[nodiscard]] bool valid_heartbeat_status(const Node node, const std::uint8_t status) noexcept {
    if ((status & kHeartbeatRunningBit) == 0U) {
        return false;
    }
    const std::uint8_t allowed =
        node == Node::hvac
            ? static_cast<std::uint8_t>(kHeartbeatRunningBit | kHeartbeatDegradedInputBit)
            : kHeartbeatRunningBit;
    return (status & ~allowed) == 0U;
}

} // namespace

bool operator==(const BmsStatus &left, const BmsStatus &right) noexcept {
    return left.voltage_deci_volts == right.voltage_deci_volts &&
           left.current_deci_amps == right.current_deci_amps &&
           left.temperature_deci_celsius == right.temperature_deci_celsius &&
           left.counter == right.counter;
}

bool operator==(const HvacStatus &left, const HvacStatus &right) noexcept {
    return left.cabin_temperature_deci_celsius == right.cabin_temperature_deci_celsius &&
           left.target_temperature_deci_celsius == right.target_temperature_deci_celsius &&
           left.fan_level == right.fan_level && left.cabin_valid == right.cabin_valid &&
           left.counter == right.counter;
}

bool operator==(const BodyStatus &left, const BodyStatus &right) noexcept {
    return left.speed_centikph == right.speed_centikph && left.door_mask == right.door_mask &&
           left.light_mask == right.light_mask &&
           left.supply_centivolts == right.supply_centivolts && left.counter == right.counter;
}

bool operator==(const Heartbeat &left, const Heartbeat &right) noexcept {
    return left.node == right.node && left.degraded_input == right.degraded_input &&
           left.uptime_ms == right.uptime_ms && left.counter == right.counter;
}

std::uint8_t app_crc8(const std::uint32_t can_id,
                      const std::array<std::uint8_t, kClassicCanDlc> &data) noexcept {
    const std::uint32_t id = arbitration_id(can_id);
    std::uint8_t crc = crc_step(kCrcInitialValue, static_cast<std::uint8_t>((id >> 8U) & 0x07U));
    crc = crc_step(crc, static_cast<std::uint8_t>(id & 0xFFU));
    for (std::size_t index{0U}; index < kClassicCanDlc - 1U; ++index) {
        crc = crc_step(crc, data.at(index));
    }
    return static_cast<std::uint8_t>(crc ^ kCrcFinalXor);
}

bool has_valid_app_crc(const Frame &frame) noexcept {
    return frame.dlc == kClassicCanDlc && frame.data.at(7U) == app_crc8(frame.can_id, frame.data);
}

Frame encode_bms_status(const BmsStatus &status) noexcept {
    Frame frame = make_frame(kBmsStatusCanId);
    write_u16_le(frame.data, 0U, status.voltage_deci_volts);
    write_i16_le(frame.data, 2U, status.current_deci_amps);
    write_i16_le(frame.data, 4U, status.temperature_deci_celsius);
    frame.data.at(6U) = status.counter;
    set_crc(frame);
    return frame;
}

Frame encode_hvac_status(const HvacStatus &status) noexcept {
    Frame frame = make_frame(kHvacStatusCanId);
    write_i16_le(frame.data, 0U, status.cabin_temperature_deci_celsius);
    write_i16_le(frame.data, 2U, status.target_temperature_deci_celsius);
    frame.data.at(4U) = status.fan_level;
    frame.data.at(5U) = status.cabin_valid ? kHvacCabinValidBit : 0U;
    frame.data.at(6U) = status.counter;
    set_crc(frame);
    return frame;
}

Frame encode_body_status(const BodyStatus &status) noexcept {
    Frame frame = make_frame(kBodyStatusCanId);
    write_u16_le(frame.data, 0U, status.speed_centikph);
    frame.data.at(2U) = status.door_mask;
    frame.data.at(3U) = status.light_mask;
    write_u16_le(frame.data, 4U, status.supply_centivolts);
    frame.data.at(6U) = status.counter;
    set_crc(frame);
    return frame;
}

Frame encode_heartbeat(const Heartbeat &heartbeat) noexcept {
    const std::uint32_t can_id = heartbeat.node == Node::bms    ? kBmsHeartbeatCanId
                                 : heartbeat.node == Node::hvac ? kHvacHeartbeatCanId
                                                                : kBodyHeartbeatCanId;
    Frame frame = make_frame(can_id);
    frame.data.at(0U) = static_cast<std::uint8_t>(heartbeat.node);
    frame.data.at(1U) = static_cast<std::uint8_t>(
        kHeartbeatRunningBit | (heartbeat.degraded_input ? kHeartbeatDegradedInputBit : 0U));
    write_u32_le(frame.data, 2U, heartbeat.uptime_ms);
    frame.data.at(6U) = heartbeat.counter;
    set_crc(frame);
    return frame;
}

DecodeResult decode_frame(const Frame &frame) noexcept {
    DecodeResult result{};
    result.message = message_type_for_arbitration_id(frame.can_id);
    result.node = node_for_message(result.message, frame.can_id);
    if (result.message != MessageType::unknown && frame.dlc >= 7U) {
        result.counter = frame.data.at(6U);
    }

    if (has_nonstandard_flags(frame.can_id)) {
        result.reason = RejectReason::invalid_flags;
        return result;
    }
    if (result.message == MessageType::unknown) {
        result.reason = RejectReason::unknown_id;
        return result;
    }
    if (frame.dlc != kClassicCanDlc) {
        result.reason = RejectReason::bad_length;
        return result;
    }
    if (!has_valid_app_crc(frame)) {
        result.reason = RejectReason::bad_crc;
        return result;
    }

    switch (result.message) {
    case MessageType::bms_status: {
        const BmsStatus status{read_u16_le(frame.data, 0U), read_i16_le(frame.data, 2U),
                               read_i16_le(frame.data, 4U), frame.data.at(6U)};
        if (!valid_bms(status)) {
            result.reason = RejectReason::invalid_payload;
            return result;
        }
        result.payload = status;
        break;
    }
    case MessageType::hvac_status: {
        const std::uint8_t status_bits = frame.data.at(5U);
        if ((status_bits & ~kHvacCabinValidBit) != 0U) {
            result.reason = RejectReason::invalid_payload;
            return result;
        }
        const HvacStatus status{read_i16_le(frame.data, 0U), read_i16_le(frame.data, 2U),
                                frame.data.at(4U), (status_bits & kHvacCabinValidBit) != 0U,
                                frame.data.at(6U)};
        if (!valid_hvac(status)) {
            result.reason = RejectReason::invalid_payload;
            return result;
        }
        result.payload = status;
        break;
    }
    case MessageType::body_status: {
        const BodyStatus status{read_u16_le(frame.data, 0U), frame.data.at(2U), frame.data.at(3U),
                                read_u16_le(frame.data, 4U), frame.data.at(6U)};
        if (!valid_body(status)) {
            result.reason = RejectReason::invalid_payload;
            return result;
        }
        result.payload = status;
        break;
    }
    case MessageType::heartbeat: {
        const std::optional<Node> expected_node = node_for_heartbeat_id(frame.can_id);
        if (!expected_node.has_value()) {
            result.reason = RejectReason::unknown_id;
            return result;
        }
        const auto encoded_node = static_cast<Node>(frame.data.at(0U));
        const std::uint8_t status_bits = frame.data.at(1U);
        if (encoded_node != *expected_node ||
            !valid_heartbeat_status(*expected_node, status_bits)) {
            result.reason = RejectReason::invalid_payload;
            return result;
        }
        result.payload = Heartbeat{*expected_node, (status_bits & kHeartbeatDegradedInputBit) != 0U,
                                   read_u32_le(frame.data, 2U), frame.data.at(6U)};
        break;
    }
    case MessageType::unknown:
        result.reason = RejectReason::unknown_id;
        return result;
    }

    result.reason = RejectReason::accepted;
    return result;
}

MessageType message_type_for_arbitration_id(const std::uint32_t can_id) noexcept {
    switch (arbitration_id(can_id)) {
    case kBmsStatusCanId:
        return MessageType::bms_status;
    case kHvacStatusCanId:
        return MessageType::hvac_status;
    case kBodyStatusCanId:
        return MessageType::body_status;
    case kBmsHeartbeatCanId:
    case kHvacHeartbeatCanId:
    case kBodyHeartbeatCanId:
        return MessageType::heartbeat;
    default:
        return MessageType::unknown;
    }
}

std::optional<Node> node_for_message(const MessageType message,
                                     const std::uint32_t can_id) noexcept {
    switch (message) {
    case MessageType::bms_status:
        return Node::bms;
    case MessageType::hvac_status:
        return Node::hvac;
    case MessageType::body_status:
        return Node::body;
    case MessageType::heartbeat:
        return node_for_heartbeat_id(can_id);
    case MessageType::unknown:
        return std::nullopt;
    }
    return std::nullopt;
}

std::string_view message_name(const MessageType message) noexcept {
    switch (message) {
    case MessageType::bms_status:
        return "bms_status";
    case MessageType::hvac_status:
        return "hvac_status";
    case MessageType::body_status:
        return "body_status";
    case MessageType::heartbeat:
        return "heartbeat";
    case MessageType::unknown:
        return "unknown";
    }
    return "unknown";
}

std::string_view node_name(const Node node) noexcept {
    switch (node) {
    case Node::bms:
        return "bms";
    case Node::hvac:
        return "hvac";
    case Node::body:
        return "body";
    }
    return "unknown";
}

std::string_view reject_reason_name(const RejectReason reason) noexcept {
    switch (reason) {
    case RejectReason::accepted:
        return "accepted";
    case RejectReason::resynchronized:
        return "resynchronized";
    case RejectReason::unknown_id:
        return "unknown_id";
    case RejectReason::bad_length:
        return "bad_length";
    case RejectReason::bad_crc:
        return "bad_crc";
    case RejectReason::invalid_flags:
        return "invalid_flags";
    case RejectReason::invalid_payload:
        return "invalid_payload";
    case RejectReason::duplicate:
        return "duplicate";
    case RejectReason::old:
        return "old";
    }
    return "unknown_id";
}

std::string data_to_upper_hex(const Frame &frame) {
    constexpr char kDigits[] = "0123456789ABCDEF";
    const std::size_t size = std::min<std::size_t>(frame.dlc, frame.data.size());
    std::string output;
    output.reserve(size * 2U);
    for (std::size_t index{0U}; index < size; ++index) {
        const std::uint8_t value = frame.data.at(index);
        output.push_back(kDigits[(value >> 4U) & 0x0FU]);
        output.push_back(kDigits[value & 0x0FU]);
    }
    return output;
}

double voltage_volts(const BmsStatus &status) noexcept {
    return static_cast<double>(status.voltage_deci_volts) / 10.0;
}

double current_amps(const BmsStatus &status) noexcept {
    return static_cast<double>(status.current_deci_amps) / 10.0;
}

double temperature_celsius(const BmsStatus &status) noexcept {
    return static_cast<double>(status.temperature_deci_celsius) / 10.0;
}

double cabin_temperature_celsius(const HvacStatus &status) noexcept {
    return static_cast<double>(status.cabin_temperature_deci_celsius) / 10.0;
}

double target_temperature_celsius(const HvacStatus &status) noexcept {
    return static_cast<double>(status.target_temperature_deci_celsius) / 10.0;
}

double speed_kph(const BodyStatus &status) noexcept {
    return static_cast<double>(status.speed_centikph) / 100.0;
}

double supply_voltage_volts(const BodyStatus &status) noexcept {
    return static_cast<double>(status.supply_centivolts) / 100.0;
}

} // namespace autodiag

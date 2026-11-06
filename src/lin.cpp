#include "autodiag/lin.hpp"

namespace autodiag {
namespace {

[[nodiscard]] constexpr std::uint8_t add_with_carry(const std::uint8_t left,
                                                    const std::uint8_t right) noexcept {
    const std::uint16_t sum = static_cast<std::uint16_t>(left) + right;
    return static_cast<std::uint8_t>((sum & 0x00FFU) + (sum >> 8U));
}

} // namespace

std::uint8_t lin_protected_id(const std::uint8_t frame_id) noexcept {
    const std::uint8_t id = frame_id & 0x3FU;
    const std::uint8_t p0 =
        static_cast<std::uint8_t>(((id >> 0U) ^ (id >> 1U) ^ (id >> 2U) ^ (id >> 4U)) & 0x01U);
    const std::uint8_t p1 =
        static_cast<std::uint8_t>(~((id >> 1U) ^ (id >> 3U) ^ (id >> 4U) ^ (id >> 5U)) & 0x01U);
    return static_cast<std::uint8_t>(id | (p0 << 6U) | (p1 << 7U));
}

std::uint8_t lin_enhanced_checksum(const std::uint8_t protected_id,
                                   const std::array<std::uint8_t, 2U> &data) noexcept {
    std::uint8_t sum = add_with_carry(protected_id, data.at(0U));
    sum = add_with_carry(sum, data.at(1U));
    return static_cast<std::uint8_t>(~sum);
}

LinTemperatureFrame encode_lin_temperature(const std::int16_t deci_celsius) noexcept {
    const std::uint16_t raw = static_cast<std::uint16_t>(deci_celsius);
    const std::array<std::uint8_t, 2U> data{
        static_cast<std::uint8_t>(raw & 0x00FFU),
        static_cast<std::uint8_t>((raw >> 8U) & 0x00FFU),
    };
    const std::uint8_t protected_id = lin_protected_id(kLinTemperatureFrameId);
    return LinTemperatureFrame{kLinTemperatureFrameId, protected_id, data,
                               lin_enhanced_checksum(protected_id, data)};
}

std::optional<std::int16_t> decode_lin_temperature(const LinTemperatureFrame &frame) noexcept {
    if (frame.frame_id != kLinTemperatureFrameId ||
        frame.protected_id != lin_protected_id(frame.frame_id) ||
        frame.checksum != lin_enhanced_checksum(frame.protected_id, frame.data)) {
        return std::nullopt;
    }
    const std::uint16_t raw =
        static_cast<std::uint16_t>(static_cast<std::uint16_t>(frame.data.at(0U)) |
                                   (static_cast<std::uint16_t>(frame.data.at(1U)) << 8U));
    return static_cast<std::int16_t>(raw);
}

} // namespace autodiag

#pragma once

#include <array>
#include <cstdint>
#include <optional>

namespace autodiag {

constexpr std::uint8_t kLinTemperatureFrameId{0x12U};

struct LinTemperatureFrame {
    std::uint8_t frame_id{kLinTemperatureFrameId};
    std::uint8_t protected_id{};
    std::array<std::uint8_t, 2U> data{};
    std::uint8_t checksum{};
};

[[nodiscard]] std::uint8_t lin_protected_id(std::uint8_t frame_id) noexcept;
[[nodiscard]] std::uint8_t lin_enhanced_checksum(std::uint8_t protected_id,
                                                 const std::array<std::uint8_t, 2U> &data) noexcept;
[[nodiscard]] LinTemperatureFrame encode_lin_temperature(std::int16_t deci_celsius) noexcept;
[[nodiscard]] std::optional<std::int16_t>
decode_lin_temperature(const LinTemperatureFrame &frame) noexcept;

} // namespace autodiag

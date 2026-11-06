#include "autodiag/lin.hpp"

#include <gtest/gtest.h>

namespace autodiag {
namespace {

TEST(lin_test, encodes_documented_enhanced_checksum_vector) {
    const LinTemperatureFrame frame = encode_lin_temperature(210);

    EXPECT_EQ(frame.frame_id, 0x12U);
    EXPECT_EQ(frame.protected_id, 0x92U);
    EXPECT_EQ(frame.data.at(0), 0xD2U);
    EXPECT_EQ(frame.data.at(1), 0x00U);
    EXPECT_EQ(frame.checksum, 0x9AU);
    EXPECT_EQ(decode_lin_temperature(frame), std::optional<std::int16_t>{210});
}

TEST(lin_test, rejects_bad_enhanced_checksum) {
    LinTemperatureFrame frame = encode_lin_temperature(210);
    frame.checksum ^= 0x01U;

    EXPECT_EQ(decode_lin_temperature(frame), std::nullopt);
}

} // namespace
} // namespace autodiag

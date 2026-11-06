#include "autodiag/protocol.hpp"

#include <gtest/gtest.h>

#include <array>
#include <cstdint>
#include <variant>

namespace autodiag {
namespace {

TEST(protocol_test, encodes_documented_bms_golden_vector) {
    const BmsStatus status{4000, 125, 320, 0};

    const Frame frame = encode_bms_status(status);

    EXPECT_EQ(frame.can_id, kBmsStatusCanId);
    EXPECT_EQ(frame.dlc, kClassicCanDlc);
    EXPECT_EQ(frame.data, (std::array<std::uint8_t, kClassicCanDlc>{0xA0U, 0x0FU, 0x7DU, 0x00U,
                                                                    0x40U, 0x01U, 0x00U, 0x9AU}));

    const DecodeResult decoded = decode_frame(frame);
    ASSERT_EQ(decoded.reason, RejectReason::accepted);
    ASSERT_TRUE(std::holds_alternative<BmsStatus>(decoded.payload));
    EXPECT_EQ(std::get<BmsStatus>(decoded.payload), status);
}

TEST(protocol_test, rejects_crc_corruption_without_decoding_payload) {
    Frame frame = encode_bms_status(BmsStatus{4000, 125, 320, 7});
    frame.data.at(7) ^= 0x01U;

    const DecodeResult decoded = decode_frame(frame);

    EXPECT_EQ(decoded.message, MessageType::bms_status);
    EXPECT_EQ(decoded.reason, RejectReason::bad_crc);
    EXPECT_TRUE(std::holds_alternative<std::monostate>(decoded.payload));
}

TEST(protocol_test, rejects_nonstandard_flags_before_using_payload) {
    Frame frame = encode_bms_status(BmsStatus{4000, 125, 320, 7});
    frame.can_id |= kCanRtrFlag;

    const DecodeResult decoded = decode_frame(frame);

    EXPECT_EQ(decoded.message, MessageType::bms_status);
    EXPECT_EQ(decoded.reason, RejectReason::invalid_flags);
}

TEST(protocol_test, validates_encodable_bms_boundaries) {
    const BmsStatus minimum{0, -2000, -400, 1};
    const BmsStatus maximum{8000, 2000, 1500, 2};

    EXPECT_EQ(decode_frame(encode_bms_status(minimum)).reason, RejectReason::accepted);
    EXPECT_EQ(decode_frame(encode_bms_status(maximum)).reason, RejectReason::accepted);

    Frame invalid = encode_bms_status(maximum);
    invalid.data.at(0) = 0x41U;
    invalid.data.at(1) = 0x1FU;
    invalid.data.at(7) = app_crc8(invalid.can_id, invalid.data);
    EXPECT_EQ(decode_frame(invalid).reason, RejectReason::invalid_payload);
}

TEST(protocol_test, encodes_and_decodes_heartbeat_with_identity) {
    const Heartbeat heartbeat{Node::hvac, false, 1000U, 42U};
    const Frame frame = encode_heartbeat(heartbeat);

    const DecodeResult decoded = decode_frame(frame);
    ASSERT_EQ(decoded.reason, RejectReason::accepted);
    ASSERT_TRUE(std::holds_alternative<Heartbeat>(decoded.payload));
    EXPECT_EQ(std::get<Heartbeat>(decoded.payload), heartbeat);
}

} // namespace
} // namespace autodiag

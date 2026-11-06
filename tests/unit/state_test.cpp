#include "autodiag/state.hpp"

#include <gtest/gtest.h>

#include <nlohmann/json.hpp>

#include <variant>

namespace autodiag {
namespace {

Frame bms_frame(std::uint8_t counter) {
    return encode_bms_status(BmsStatus{4000, 125, 320, counter});
}

Frame bms_heartbeat(std::uint32_t uptime_ms, std::uint8_t counter) {
    return encode_heartbeat(Heartbeat{Node::bms, false, uptime_ms, counter});
}

TEST(state_test, accepts_counter_wraparound) {
    MonitorState state;

    EXPECT_EQ(state.ingest(bms_frame(255U), 0U).reason, RejectReason::accepted);
    EXPECT_EQ(state.ingest(bms_frame(0U), 100U).reason, RejectReason::accepted);
    EXPECT_EQ(state.snapshot(100U).nodes.at(Node::bms).data_counter,
              std::optional<std::uint8_t>{0U});
}

TEST(state_test, duplicate_is_rejected_even_after_data_becomes_stale) {
    MonitorState state;
    ASSERT_EQ(state.ingest(bms_frame(7U), 0U).reason, RejectReason::accepted);

    EXPECT_EQ(state.snapshot(500U).nodes.at(Node::bms).data_status, DataStatus::stale);
    EXPECT_EQ(state.ingest(bms_frame(7U), 600U).reason, RejectReason::duplicate);

    const MonitorSnapshot snapshot = state.snapshot(600U);
    EXPECT_EQ(snapshot.nodes.at(Node::bms).data_status, DataStatus::stale);
    EXPECT_EQ(snapshot.nodes.at(Node::bms).rejections.duplicate, 1U);
}

TEST(state_test, epoch_change_clears_data_baseline_and_allows_new_data) {
    MonitorState state;
    ASSERT_EQ(state.ingest(bms_heartbeat(1000U, 1U), 0U).reason, RejectReason::accepted);
    ASSERT_EQ(state.ingest(bms_frame(9U), 0U).reason, RejectReason::accepted);

    EXPECT_EQ(state.ingest(bms_heartbeat(5U, 1U), 100U).reason, RejectReason::resynchronized);
    const MonitorSnapshot after_restart = state.snapshot(100U);
    EXPECT_EQ(after_restart.nodes.at(Node::bms).data_status, DataStatus::missing);
    EXPECT_FALSE(after_restart.nodes.at(Node::bms).signals.has_value());
    EXPECT_EQ(after_restart.nodes.at(Node::bms).resynchronizations, 1U);

    EXPECT_EQ(state.ingest(bms_frame(9U), 101U).reason, RejectReason::accepted);
}

TEST(state_test, crc_error_does_not_refresh_data_freshness) {
    MonitorState state;
    ASSERT_EQ(state.ingest(bms_frame(1U), 0U).reason, RejectReason::accepted);

    Frame corrupt = bms_frame(2U);
    corrupt.data.at(7) ^= 0x01U;
    EXPECT_EQ(state.ingest(corrupt, 400U).reason, RejectReason::bad_crc);

    const MonitorSnapshot snapshot = state.snapshot(500U);
    EXPECT_EQ(snapshot.nodes.at(Node::bms).data_status, DataStatus::stale);
    EXPECT_EQ(snapshot.nodes.at(Node::bms).rejections.bad_crc, 1U);
}

TEST(state_test, heartbeat_and_data_freshness_are_independent) {
    MonitorState state;
    ASSERT_EQ(state.ingest(bms_heartbeat(0U, 0U), 0U).reason, RejectReason::accepted);
    ASSERT_EQ(state.ingest(bms_frame(0U), 0U).reason, RejectReason::accepted);
    ASSERT_EQ(state.ingest(bms_heartbeat(750U, 3U), 750U).reason, RejectReason::accepted);

    const MonitorSnapshot snapshot = state.snapshot(750U);
    EXPECT_TRUE(snapshot.nodes.at(Node::bms).online);
    EXPECT_EQ(snapshot.nodes.at(Node::bms).data_status, DataStatus::stale);
}

TEST(state_test, serializes_optional_scalars_as_scalars_or_null) {
    MonitorState state;
    const nlohmann::json empty = state.snapshot(0U);
    const nlohmann::json &empty_bms = empty.at("nodes").at("bms");
    EXPECT_TRUE(empty_bms.at("data_age_ms").is_null());
    EXPECT_TRUE(empty_bms.at("signals").is_null());
    EXPECT_TRUE(empty_bms.at("data_status").is_string());

    ASSERT_EQ(state.ingest(bms_frame(9U), 100U).reason, RejectReason::accepted);
    const nlohmann::json populated = state.snapshot(200U);
    const nlohmann::json &bms = populated.at("nodes").at("bms");
    EXPECT_TRUE(bms.at("data_age_ms").is_number_unsigned());
    EXPECT_TRUE(bms.at("data_counter").is_number_unsigned());
    EXPECT_TRUE(bms.at("signals").is_object());
    EXPECT_TRUE(bms.at("signals").at("temperature_c").is_number());
}

} // namespace
} // namespace autodiag

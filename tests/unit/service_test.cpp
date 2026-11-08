#include "autodiag/service.hpp"

#include <chrono>
#include <cstdint>
#include <gtest/gtest.h>

namespace autodiag {
namespace {
using namespace std::chrono_literals;

nlohmann::json request(std::string method, nlohmann::json params = nlohmann::json::object()) {
    return {{"version", 1},
            {"id", "unit-1"},
            {"method", std::move(method)},
            {"params", std::move(params)}};
}

TEST(service_test, reports_missing_data_without_fabrication) {
    LiveService service{"vcan0", [] { return 50U; }};
    const auto response =
        service.dispatch(request("snapshot"), std::chrono::steady_clock::now() + 1s);
    ASSERT_TRUE(response["ok"].get<bool>());
    EXPECT_EQ(response["result"]["nodes"]["bms"]["data_status"], "missing");
    EXPECT_TRUE(response["result"]["nodes"]["bms"]["signals"].is_null());
    EXPECT_TRUE(response["result"]["evidence_id"].is_string());
    EXPECT_EQ(response["result"]["observed_at_ms"], 50);
}

TEST(service_test, history_is_bounded_and_rejected_frames_are_not_samples) {
    std::uint64_t now{};
    LiveService service{"vcan0", [&] { return now; }};
    for (std::uint64_t tick = 0; tick < 600; ++tick) {
        now = tick * 100;
        service.ingest(encode_bms_status({4000U, 125, 320, static_cast<std::uint8_t>(tick)}), now);
    }
    service.ingest(encode_bms_status({4000U, 125, 320, static_cast<std::uint8_t>(599)}), now);
    const auto response = service.dispatch(request("signal_window", {{"node", "bms"},
                                                                     {"signal", "temperature_c"},
                                                                     {"window_ms", 60000},
                                                                     {"max_samples", 10}}),
                                           std::chrono::steady_clock::now() + 1s);
    ASSERT_TRUE(response["ok"].get<bool>());
    const auto &result = response["result"];
    EXPECT_EQ(result["sample_count"], 512);
    EXPECT_EQ(result["returned_count"], 10);
    EXPECT_TRUE(result["truncated"].get<bool>());
    EXPECT_EQ(result["samples"][0]["t_ms"], 59000);
    EXPECT_EQ(result["samples"][9]["value"], 32.0);
}

TEST(service_test, rejects_invalid_envelopes_and_parameters) {
    LiveService service{"vcan0"};
    const auto deadline = std::chrono::steady_clock::now() + 1s;
    auto invalid = request("snapshot");
    invalid["version"] = true;
    EXPECT_EQ(service.dispatch(invalid, deadline)["error"]["code"], "invalid_request");
    EXPECT_EQ(service.dispatch(request("unknown"), deadline)["error"]["code"], "method_not_found");
    EXPECT_EQ(service.dispatch(request("snapshot", {{"extra", 1}}), deadline)["error"]["code"],
              "invalid_params");
    EXPECT_EQ(service.dispatch(request("read_did", {{"node", "bms"}, {"did", true}}),
                               deadline)["error"]["code"],
              "invalid_params");
}
} // namespace
} // namespace autodiag

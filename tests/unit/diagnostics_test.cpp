#include "autodiag/diagnostics.hpp"
#include "autodiag/isotp.hpp"

#include <gtest/gtest.h>

#include <cstdint>
#include <stdexcept>
#include <vector>

namespace autodiag {
namespace {

const Frame normal_bms = encode_bms_status({4000U, -125, 320, 0U});

TEST(diagnostics_test, maps_nodes_to_physical_isotp_addresses) {
    EXPECT_EQ(diagnostic_request_id(Node::bms), 0x7E0U);
    EXPECT_EQ(diagnostic_request_id(Node::hvac), 0x7E1U);
    EXPECT_EQ(diagnostic_response_id(Node::body), 0x7EAU);
    EXPECT_THROW(static_cast<void>(diagnostic_request_id(static_cast<Node>(0))),
                 std::invalid_argument);
}

TEST(diagnostics_test, identity_is_available_in_default_session_and_requires_multiframe) {
    UdsServer server{Node::bms};
    const std::vector<std::uint8_t> expected{0x62, 0xF1, 0x80, 'A', 'u', 't', 'o', 'D',
                                             'i',  'a',  'g',  '/', '1', '.', '0'};
    EXPECT_EQ(server.handle({0x22, 0xF1, 0x80}, 0U, {}), expected);
    EXPECT_GT(expected.size(), 7U);
    EXPECT_EQ(server.handle({0x22, 0xF1, 0x87}, 1U, {}),
              (std::vector<std::uint8_t>{0x62, 0xF1, 0x87, 'b', 'm', 's'}));
}

TEST(diagnostics_test, signal_reads_require_extended_session_and_preserve_wire_endianness) {
    UdsServer server{Node::bms};
    EXPECT_EQ(server.handle({0x22, 0xD1, 0x00}, 0U, normal_bms),
              (std::vector<std::uint8_t>{0x7F, 0x22, 0x31}));
    EXPECT_EQ(server.handle({0x10, 0x03}, 1U, normal_bms),
              (std::vector<std::uint8_t>{0x50, 0x03, 0x00, 0xFA, 0x00, 0xC8}));
    EXPECT_EQ(server.handle({0x22, 0xD1, 0x00}, 2U, normal_bms),
              (std::vector<std::uint8_t>{0x62, 0xD1, 0x00, 0xA0, 0x0F}));
    EXPECT_EQ(server.handle({0x22, 0xD1, 0x01}, 3U, normal_bms),
              (std::vector<std::uint8_t>{0x62, 0xD1, 0x01, 0x83, 0xFF}));
}

TEST(diagnostics_test, tester_present_refreshes_session_but_invalid_requests_do_not) {
    UdsServer server{Node::bms};
    static_cast<void>(server.handle({0x10, 0x03}, 0U, normal_bms));
    EXPECT_EQ(server.handle({0x3E, 0x00}, 4999U, normal_bms),
              (std::vector<std::uint8_t>{0x7E, 0x00}));
    EXPECT_EQ(server.handle({0x22, 0xD1, 0x02}, 9998U, normal_bms).front(), 0x62);
    EXPECT_EQ(server.handle({0x99}, 14997U, normal_bms),
              (std::vector<std::uint8_t>{0x7F, 0x99, 0x11}));
    EXPECT_EQ(server.handle({0x22, 0xD1, 0x02}, 14998U, normal_bms),
              (std::vector<std::uint8_t>{0x7F, 0x22, 0x31}));
    EXPECT_EQ(server.session(), 0x01);
}

TEST(diagnostics_test, default_session_request_resets_access) {
    UdsServer server{Node::bms};
    static_cast<void>(server.handle({0x10, 0x03}, 0U, normal_bms));
    static_cast<void>(server.handle({0x10, 0x01}, 1U, normal_bms));
    EXPECT_EQ(server.handle({0x22, 0xD1, 0x02}, 2U, normal_bms),
              (std::vector<std::uint8_t>{0x7F, 0x22, 0x31}));
}

TEST(diagnostics_test, returns_precise_negative_responses) {
    UdsServer server{Node::bms};
    EXPECT_EQ(server.handle({}, 0U, normal_bms), (std::vector<std::uint8_t>{0x7F, 0x00, 0x13}));
    EXPECT_EQ(server.handle({0x10}, 0U, normal_bms), (std::vector<std::uint8_t>{0x7F, 0x10, 0x13}));
    EXPECT_EQ(server.handle({0x10, 0x02}, 0U, normal_bms),
              (std::vector<std::uint8_t>{0x7F, 0x10, 0x12}));
    EXPECT_EQ(server.handle({0x22, 0xF1, 0x80, 0}, 0U, normal_bms),
              (std::vector<std::uint8_t>{0x7F, 0x22, 0x13}));
    EXPECT_EQ(server.handle({0x22, 0xAB, 0xCD}, 0U, normal_bms),
              (std::vector<std::uint8_t>{0x7F, 0x22, 0x31}));
    EXPECT_EQ(server.handle({0x19, 0x01, 0xFF}, 0U, normal_bms),
              (std::vector<std::uint8_t>{0x7F, 0x19, 0x12}));
    EXPECT_EQ(server.handle({0x3E, 0x80}, 0U, normal_bms),
              (std::vector<std::uint8_t>{0x7F, 0x3E, 0x12}));
}

TEST(diagnostics_test, dtcs_are_derived_from_current_model_not_injector_labels) {
    UdsServer server{Node::bms};
    const Frame hot = encode_bms_status({4000U, 125, 1250, 0U});
    EXPECT_EQ(server.handle({0x19, 0x02, 0xFF}, 0U, hot),
              (std::vector<std::uint8_t>{0x59, 0x02, 0x09, 0xA1, 0x00, 0x01, 0x09}));
    EXPECT_EQ(server.handle({0x19, 0x02, 0x00}, 1U, hot),
              (std::vector<std::uint8_t>{0x59, 0x02, 0x09}));
    EXPECT_EQ(server.handle({0x19, 0x02, 0x04}, 2U, hot),
              (std::vector<std::uint8_t>{0x59, 0x02, 0x09}));
    EXPECT_EQ(server.handle({0x19, 0x02, 0xFF}, 3U, normal_bms),
              (std::vector<std::uint8_t>{0x59, 0x02, 0x09}));
}

TEST(diagnostics_test, hvac_and_body_catalogs_preserve_validity_and_bits) {
    UdsServer hvac{Node::hvac};
    const Frame cabin = encode_hvac_status({-50, 220, 2U, false, 0U});
    static_cast<void>(hvac.handle({0x10, 0x03}, 0U, cabin));
    EXPECT_EQ(hvac.handle({0x22, 0xD2, 0x00}, 1U, cabin),
              (std::vector<std::uint8_t>{0x62, 0xD2, 0x00, 0xCE, 0xFF, 0x00}));
    EXPECT_EQ(hvac.handle({0x22, 0xD2, 0x01}, 2U, cabin),
              (std::vector<std::uint8_t>{0x62, 0xD2, 0x01, 0xDC, 0x00}));
    EXPECT_EQ(hvac.handle({0x22, 0xD2, 0x02}, 3U, cabin),
              (std::vector<std::uint8_t>{0x62, 0xD2, 0x02, 0x02, 0x00}));
    EXPECT_EQ(hvac.handle({0x19, 0x02, 0x09}, 4U, cabin),
              (std::vector<std::uint8_t>{0x59, 0x02, 0x09, 0xA2, 0x00, 0x01, 0x09}));
    UdsServer body{Node::body};
    const Frame vehicle = encode_body_status({1234U, 31U, 7U, 1250U, 0U});
    static_cast<void>(body.handle({0x10, 0x03}, 0U, vehicle));
    EXPECT_EQ(body.handle({0x22, 0xD3, 0x00}, 1U, vehicle),
              (std::vector<std::uint8_t>{0x62, 0xD3, 0x00, 0xD2, 0x04}));
    EXPECT_EQ(body.handle({0x22, 0xD3, 0x01}, 2U, vehicle),
              (std::vector<std::uint8_t>{0x62, 0xD3, 0x01, 31, 7}));
    EXPECT_EQ(body.handle({0x22, 0xD3, 0x02}, 3U, vehicle),
              (std::vector<std::uint8_t>{0x62, 0xD3, 0x02, 0xE2, 0x04}));
}

TEST(diagnostics_test, rejects_bad_internal_sample_without_fabricating_measurements) {
    UdsServer server{Node::bms};
    static_cast<void>(server.handle({0x10, 0x03}, 0U, {}));
    EXPECT_EQ(server.handle({0x22, 0xD1, 0x02}, 1U, {}),
              (std::vector<std::uint8_t>{0x7F, 0x22, 0x22}));
    EXPECT_EQ(server.handle({0x19, 0x02, 0xFF}, 1U, {}),
              (std::vector<std::uint8_t>{0x7F, 0x19, 0x22}));
}

TEST(diagnostics_test, transport_refuses_nonvirtual_interfaces_and_bad_client_input) {
    EXPECT_THROW(static_cast<void>(IsotpSocket::open_server("lo", Node::bms)),
                 std::invalid_argument);
    EXPECT_THROW(
        static_cast<void>(isotp_request("lo", Node::bms, {0x3E, 0}, std::chrono::milliseconds{20})),
        std::invalid_argument);
}

} // namespace
} // namespace autodiag

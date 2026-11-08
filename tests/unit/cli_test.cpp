#include "autodiag/cli.hpp"

#include <gtest/gtest.h>

#include <string>
#include <vector>

namespace autodiag {
namespace {

[[nodiscard]] std::vector<char *> argv_for(std::vector<std::string> &arguments) {
    std::vector<char *> pointers;
    pointers.reserve(arguments.size());
    for (std::string &argument : arguments) {
        pointers.push_back(argument.data());
    }
    return pointers;
}

[[nodiscard]] EcuOptions parse_ecu(std::vector<std::string> arguments) {
    std::vector<char *> argv = argv_for(arguments);
    return parse_ecu_options(static_cast<int>(argv.size()), argv.data());
}

TEST(cli_test, accepts_uint32_seed_boundary) {
    const EcuOptions options = parse_ecu({"autodiag-ecu", "--node", "bms", "--seed", "4294967295"});
    EXPECT_EQ(options.seed, 4294967295U);
}

TEST(cli_test, rejects_counter_that_does_not_fit_in_uint8) {
    EXPECT_THROW(parse_ecu({"autodiag-ecu", "--node", "bms", "--counter-start", "256"}),
                 UsageError);
}

TEST(cli_test, rejects_seed_that_does_not_fit_in_uint32) {
    EXPECT_THROW(parse_ecu({"autodiag-ecu", "--node", "bms", "--seed", "4294967296"}), UsageError);
}

TEST(cli_test, rejects_uint64_overflow_and_negative_values) {
    EXPECT_THROW(
        parse_ecu({"autodiag-ecu", "--node", "bms", "--duration-ms", "18446744073709551616"}),
        UsageError);
    EXPECT_THROW(parse_ecu({"autodiag-ecu", "--node", "bms", "--seed", "-1"}), UsageError);
}

TEST(cli_test, rejects_missing_values_and_invalid_fault_node_combinations) {
    EXPECT_THROW(parse_ecu({"autodiag-ecu", "--node"}), UsageError);
    EXPECT_THROW(parse_ecu({"autodiag-ecu", "--node", "hvac", "--fault", "overtemperature"}),
                 UsageError);
    EXPECT_THROW(parse_ecu({"autodiag-ecu", "--node", "bms", "--fault", "lin_bad_checksum"}),
                 UsageError);
}

TEST(cli_test, accepts_old_sequence_wire_fault_for_any_node) {
    const EcuOptions options =
        parse_ecu({"autodiag-ecu", "--node", "body", "--fault", "old_sequence"});
    EXPECT_EQ(options.fault, Fault::old_sequence);
    EXPECT_EQ(fault_name(options.fault), "old_sequence");
}

TEST(cli_test, restricts_wrong_scale_wire_fault_to_bms) {
    const EcuOptions options =
        parse_ecu({"autodiag-ecu", "--node", "bms", "--fault", "wrong_scale"});
    EXPECT_EQ(options.fault, Fault::wrong_scale);
    EXPECT_EQ(fault_name(options.fault), "wrong_scale");
    EXPECT_THROW(parse_ecu({"autodiag-ecu", "--node", "hvac", "--fault", "wrong_scale"}),
                 UsageError);
    EXPECT_THROW(parse_ecu({"autodiag-ecu", "--node", "body", "--fault", "wrong_scale"}),
                 UsageError);
}

TEST(cli_test, wire_fault_activation_uses_the_existing_half_open_window) {
    const EcuOptions options =
        parse_ecu({"autodiag-ecu", "--node", "bms", "--fault", "old_sequence", "--fault-after-ms",
                   "400", "--fault-duration-ms", "400"});
    EXPECT_FALSE(fault_is_active(options, 399U));
    EXPECT_TRUE(fault_is_active(options, 400U));
    EXPECT_TRUE(fault_is_active(options, 799U));
    EXPECT_FALSE(fault_is_active(options, 800U));
}

} // namespace
} // namespace autodiag

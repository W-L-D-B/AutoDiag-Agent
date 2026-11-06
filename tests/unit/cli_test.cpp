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

} // namespace
} // namespace autodiag

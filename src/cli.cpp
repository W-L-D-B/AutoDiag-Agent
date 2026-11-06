#include "autodiag/cli.hpp"

#include <charconv>
#include <cstddef>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>

namespace autodiag {
namespace {

[[nodiscard]] std::string_view next_value(const int argc, char *argv[], int &index,
                                          const std::string_view option) {
    if (index + 1 >= argc) {
        throw UsageError{"missing value for " + std::string{option}};
    }
    ++index;
    return argv[index];
}

struct NamedOptionValue {
    std::string_view option;
    std::string_view value;
};

template <typename Integer> [[nodiscard]] Integer parse_unsigned(const NamedOptionValue input) {
    Integer parsed{};
    const char *const begin = input.value.data();
    const char *const end = begin + input.value.size();
    const auto [position, error] = std::from_chars(begin, end, parsed);
    if (error != std::errc{} || position != end) {
        throw UsageError{"invalid unsigned value for " + std::string{input.option}};
    }
    return parsed;
}

[[nodiscard]] Node parse_node(const std::string_view value) {
    if (value == "bms") {
        return Node::bms;
    }
    if (value == "hvac") {
        return Node::hvac;
    }
    if (value == "body") {
        return Node::body;
    }
    throw UsageError{"--node must be bms, hvac, or body"};
}

[[nodiscard]] Fault parse_fault(const std::string_view value) {
    if (value == "none") {
        return Fault::none;
    }
    if (value == "drop_data") {
        return Fault::drop_data;
    }
    if (value == "bad_crc") {
        return Fault::bad_crc;
    }
    if (value == "freeze_counter") {
        return Fault::freeze_counter;
    }
    if (value == "overtemperature") {
        return Fault::overtemperature;
    }
    if (value == "lin_bad_checksum") {
        return Fault::lin_bad_checksum;
    }
    if (value == "exit") {
        return Fault::exit_process;
    }
    throw UsageError{"invalid --fault value"};
}

void validate_fault(const EcuOptions &options) {
    if (options.fault == Fault::overtemperature && options.node != Node::bms) {
        throw UsageError{"overtemperature fault only applies to bms"};
    }
    if (options.fault == Fault::lin_bad_checksum && options.node != Node::hvac) {
        throw UsageError{"lin_bad_checksum fault only applies to hvac"};
    }
}

} // namespace

EcuOptions parse_ecu_options(const int argc, char *argv[]) {
    EcuOptions options{};
    bool has_node{};
    for (int index{1}; index < argc; ++index) {
        const std::string_view option{argv[index]};
        if (option == "--node") {
            options.node = parse_node(next_value(argc, argv, index, option));
            has_node = true;
        } else if (option == "--interface") {
            options.interface_name = next_value(argc, argv, index, option);
        } else if (option == "--duration-ms") {
            options.duration_ms = parse_unsigned<std::uint64_t>(
                NamedOptionValue{option, next_value(argc, argv, index, option)});
        } else if (option == "--seed") {
            options.seed = parse_unsigned<std::uint32_t>(
                NamedOptionValue{option, next_value(argc, argv, index, option)});
        } else if (option == "--counter-start") {
            options.counter_start = parse_unsigned<std::uint8_t>(
                NamedOptionValue{option, next_value(argc, argv, index, option)});
        } else if (option == "--fault") {
            options.fault = parse_fault(next_value(argc, argv, index, option));
        } else if (option == "--fault-after-ms") {
            options.fault_after_ms = parse_unsigned<std::uint64_t>(
                NamedOptionValue{option, next_value(argc, argv, index, option)});
        } else if (option == "--fault-duration-ms") {
            options.fault_duration_ms = parse_unsigned<std::uint64_t>(
                NamedOptionValue{option, next_value(argc, argv, index, option)});
        } else {
            throw UsageError{"unknown ecu option: " + std::string{option}};
        }
    }
    if (!has_node) {
        throw UsageError{"--node is required"};
    }
    if (options.duration_ms == 0U) {
        throw UsageError{"--duration-ms must be greater than zero"};
    }
    validate_fault(options);
    return options;
}

MonitorOptions parse_monitor_options(const int argc, char *argv[]) {
    MonitorOptions options{};
    for (int index{1}; index < argc; ++index) {
        const std::string_view option{argv[index]};
        if (option == "--interface") {
            options.interface_name = next_value(argc, argv, index, option);
        } else if (option == "--duration-ms") {
            options.duration_ms = parse_unsigned<std::uint64_t>(
                NamedOptionValue{option, next_value(argc, argv, index, option)});
        } else if (option == "--snapshot") {
            options.snapshot_path = next_value(argc, argv, index, option);
        } else if (option == "--log") {
            options.log_path = next_value(argc, argv, index, option);
        } else {
            throw UsageError{"unknown monitor option: " + std::string{option}};
        }
    }
    if (options.duration_ms == 0U) {
        throw UsageError{"--duration-ms must be greater than zero"};
    }
    if (options.snapshot_path.empty() || options.log_path.empty()) {
        throw UsageError{"--snapshot and --log are required"};
    }
    return options;
}

bool fault_is_active(const EcuOptions &options, const std::uint64_t elapsed_ms) noexcept {
    if (options.fault == Fault::none || elapsed_ms < options.fault_after_ms) {
        return false;
    }
    return options.fault_duration_ms == 0U ||
           elapsed_ms - options.fault_after_ms < options.fault_duration_ms;
}

std::string_view fault_name(const Fault fault) noexcept {
    switch (fault) {
    case Fault::none:
        return "none";
    case Fault::drop_data:
        return "drop_data";
    case Fault::bad_crc:
        return "bad_crc";
    case Fault::freeze_counter:
        return "freeze_counter";
    case Fault::overtemperature:
        return "overtemperature";
    case Fault::lin_bad_checksum:
        return "lin_bad_checksum";
    case Fault::exit_process:
        return "exit";
    }
    return "none";
}

} // namespace autodiag

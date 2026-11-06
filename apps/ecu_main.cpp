#include "autodiag/cli.hpp"
#include "autodiag/lin.hpp"
#include "autodiag/protocol.hpp"
#include "autodiag/socketcan.hpp"

#include <algorithm>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <exception>
#include <iostream>
#include <optional>
#include <thread>

namespace {

volatile std::sig_atomic_t g_stop_requested{0};

extern "C" void request_stop(int) {
    g_stop_requested = 1;
}

[[nodiscard]] std::uint8_t counter_at(const std::uint8_t initial,
                                      const std::uint64_t tick) noexcept {
    return static_cast<std::uint8_t>(initial + static_cast<std::uint8_t>(tick));
}

[[nodiscard]] std::int16_t sample_offset(const std::uint32_t seed,
                                         const std::uint64_t tick) noexcept {
    constexpr std::uint32_t kSamplePeriod{5U};
    const std::uint32_t value =
        (seed % kSamplePeriod + static_cast<std::uint32_t>(tick % kSamplePeriod) +
         (kSamplePeriod - 1U)) %
        kSamplePeriod;
    return static_cast<std::int16_t>(value);
}

[[nodiscard]] autodiag::Frame make_data_frame(const autodiag::EcuOptions &options,
                                              const std::uint64_t tick, const bool fault_active,
                                              std::optional<std::uint8_t> &frozen_counter,
                                              std::int16_t &last_good_hvac_temperature) {
    const std::int16_t offset = sample_offset(options.seed, tick);
    std::uint8_t counter = counter_at(options.counter_start, tick);
    if (fault_active && options.fault == autodiag::Fault::freeze_counter) {
        if (!frozen_counter.has_value()) {
            frozen_counter = counter;
        }
        counter = *frozen_counter;
    } else {
        frozen_counter.reset();
    }

    switch (options.node) {
    case autodiag::Node::bms: {
        std::int16_t temperature = static_cast<std::int16_t>(320 + offset);
        if (fault_active && options.fault == autodiag::Fault::overtemperature) {
            temperature = static_cast<std::int16_t>(1250);
        }
        return autodiag::encode_bms_status(
            autodiag::BmsStatus{static_cast<std::uint16_t>(4000 + offset),
                                static_cast<std::int16_t>(125 + offset), temperature, counter});
    }
    case autodiag::Node::hvac: {
        autodiag::LinTemperatureFrame lin =
            autodiag::encode_lin_temperature(static_cast<std::int16_t>(210 + offset));
        if (fault_active && options.fault == autodiag::Fault::lin_bad_checksum) {
            lin.checksum ^= 0x01U;
        }
        const std::optional<std::int16_t> decoded_temperature =
            autodiag::decode_lin_temperature(lin);
        const bool cabin_valid = decoded_temperature.has_value();
        if (cabin_valid) {
            last_good_hvac_temperature = *decoded_temperature;
        }
        return autodiag::encode_hvac_status(
            autodiag::HvacStatus{last_good_hvac_temperature, 220, 2U, cabin_valid, counter});
    }
    case autodiag::Node::body:
        return autodiag::encode_body_status(autodiag::BodyStatus{
            static_cast<std::uint16_t>(1234 + offset), 0x03U, 0x01U, 1234U, counter});
    }
    std::terminate();
}

[[nodiscard]] autodiag::Frame make_heartbeat(const autodiag::EcuOptions &options,
                                             const std::uint64_t uptime_ms,
                                             const std::uint64_t tick, const bool fault_active) {
    const bool degraded_input = options.node == autodiag::Node::hvac && fault_active &&
                                options.fault == autodiag::Fault::lin_bad_checksum;
    return autodiag::encode_heartbeat(autodiag::Heartbeat{options.node, degraded_input,
                                                          static_cast<std::uint32_t>(uptime_ms),
                                                          counter_at(options.counter_start, tick)});
}

[[nodiscard]] std::uint64_t elapsed_ms(const std::chrono::steady_clock::time_point start) {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
                                          std::chrono::steady_clock::now() - start)
                                          .count());
}

int run(const autodiag::EcuOptions &options) {
    autodiag::SocketCan socket = autodiag::SocketCan::open_virtual(options.interface_name);
    const auto start = std::chrono::steady_clock::now();
    std::uint64_t next_data_ms{};
    std::uint64_t next_heartbeat_ms{};
    std::uint64_t data_tick{};
    std::uint64_t heartbeat_tick{};
    std::optional<std::uint8_t> frozen_counter{};
    std::int16_t last_good_hvac_temperature{210};

    while (g_stop_requested == 0) {
        const std::uint64_t now_ms = elapsed_ms(start);
        if (now_ms >= options.duration_ms) {
            break;
        }

        while (next_data_ms <= now_ms && next_data_ms < options.duration_ms) {
            const bool active = autodiag::fault_is_active(options, next_data_ms);
            if (active && options.fault == autodiag::Fault::exit_process) {
                return 42;
            }
            if (!(active && options.fault == autodiag::Fault::drop_data)) {
                autodiag::Frame frame = make_data_frame(options, data_tick, active, frozen_counter,
                                                        last_good_hvac_temperature);
                if (active && options.fault == autodiag::Fault::bad_crc) {
                    frame.data.at(7U) ^= 0x01U;
                }
                socket.send(frame);
            }
            ++data_tick;
            next_data_ms += 100U;
        }

        while (next_heartbeat_ms <= now_ms && next_heartbeat_ms < options.duration_ms) {
            const bool active = autodiag::fault_is_active(options, next_heartbeat_ms);
            if (active && options.fault == autodiag::Fault::exit_process) {
                return 42;
            }
            socket.send(make_heartbeat(options, elapsed_ms(start), heartbeat_tick, active));
            ++heartbeat_tick;
            next_heartbeat_ms += 250U;
        }

        const std::uint64_t next_event_ms = std::min(next_data_ms, next_heartbeat_ms);
        const std::uint64_t remaining_ms = next_event_ms > now_ms ? next_event_ms - now_ms : 1U;
        std::this_thread::sleep_for(
            std::chrono::milliseconds{std::min<std::uint64_t>(remaining_ms, 10U)});
    }
    return 0;
}

} // namespace

int main(int argc, char *argv[]) {
    std::signal(SIGINT, request_stop);
    std::signal(SIGTERM, request_stop);
    try {
        return run(autodiag::parse_ecu_options(argc, argv));
    } catch (const autodiag::UsageError &error) {
        std::cerr << "usage error: " << error.what() << '\n';
        return 2;
    } catch (const std::exception &error) {
        std::cerr << "autodiag-ecu: " << error.what() << '\n';
        return 1;
    }
}

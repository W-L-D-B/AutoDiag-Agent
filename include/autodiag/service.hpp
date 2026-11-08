#pragma once

#include "autodiag/framed_rpc.hpp"
#include "autodiag/protocol.hpp"
#include "autodiag/state.hpp"

#include <array>
#include <atomic>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>

namespace autodiag {

class LiveService {
  public:
    using Clock = std::function<std::uint64_t()>;
    explicit LiveService(std::string interface_name, Clock clock = {});
    ~LiveService() noexcept;
    LiveService(const LiveService &) = delete;
    LiveService &operator=(const LiveService &) = delete;
    LiveService(LiveService &&) = delete;
    LiveService &operator=(LiveService &&) = delete;
    void start();
    void stop() noexcept;
    [[nodiscard]] nlohmann::json dispatch(const nlohmann::json &request, RpcDeadline deadline);
    // A clock/input seam shared by the live collector and deterministic tests.
    void ingest(const Frame &frame, std::uint64_t now_ms);

  private:
    struct Sample {
        std::uint64_t at_ms;
        nlohmann::json signals;
        std::string evidence_id;
    };
    struct History {
        std::deque<Sample> samples;
        std::optional<std::uint64_t> discarded_through;
    };

    [[nodiscard]] std::string evidence(std::string_view kind);
    [[nodiscard]] nlohmann::json execute(std::string_view method, const nlohmann::json &params,
                                         RpcDeadline deadline);
    [[nodiscard]] nlohmann::json read_did(Node node, std::uint16_t did, RpcDeadline deadline);
    [[nodiscard]] nlohmann::json read_dtcs(Node node, std::uint8_t mask, RpcDeadline deadline);
    [[nodiscard]] std::vector<std::uint8_t> exchange(Node node,
                                                     const std::vector<std::uint8_t> &request,
                                                     std::string_view phase, RpcDeadline deadline);
    [[nodiscard]] nlohmann::json snapshot(std::uint64_t now_ms);

    const std::string interface_name_;
    const std::string boot_id_;
    Clock clock_;
    std::atomic<std::uint64_t> evidence_sequence_{0};
    std::atomic<bool> stopping_{false};
    std::atomic<bool> collector_running_{false};
    std::thread collector_;
    std::mutex state_mutex_;
    MonitorState state_;
    std::array<History, 3> history_{};
    std::array<std::timed_mutex, 3> tester_mutexes_{};
    // Protected by the corresponding tester mutex. UDS has no transaction ID:
    // after timeout, only a controlled full-stack restart can restore correlation.
    std::array<bool, 3> tester_quarantined_{};
};

} // namespace autodiag

#pragma once

#include "autodiag/protocol.hpp"

#include <cstdint>
#include <optional>
#include <vector>

namespace autodiag {

constexpr std::uint64_t kDiagnosticSessionTimeoutMs{5000U};

[[nodiscard]] std::uint32_t diagnostic_request_id(Node node);
[[nodiscard]] std::uint32_t diagnostic_response_id(Node node);

class UdsServer {
  public:
    explicit UdsServer(Node node);

    [[nodiscard]] std::vector<std::uint8_t> handle(const std::vector<std::uint8_t> &request,
                                                   std::uint64_t now_ms, const Frame &clean_sample);
    [[nodiscard]] std::uint8_t session() const noexcept;

  private:
    Node node_;
    std::uint8_t session_{0x01U};
    std::optional<std::uint64_t> last_valid_request_ms_{};
};

} // namespace autodiag

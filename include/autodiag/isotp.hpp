#pragma once

#include "autodiag/protocol.hpp"

#include <chrono>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace autodiag {

class IsotpTimeout : public std::runtime_error {
  public:
    using std::runtime_error::runtime_error;
};

class IsotpSocket {
  public:
    IsotpSocket() = delete;
    ~IsotpSocket() noexcept;
    IsotpSocket(const IsotpSocket &) = delete;
    IsotpSocket &operator=(const IsotpSocket &) = delete;
    IsotpSocket(IsotpSocket &&other) noexcept;
    IsotpSocket &operator=(IsotpSocket &&other) noexcept;

    [[nodiscard]] static IsotpSocket open_server(std::string_view interface_name, Node node);
    [[nodiscard]] static IsotpSocket open_client(std::string_view interface_name, Node node);
    [[nodiscard]] bool try_send(const std::vector<std::uint8_t> &payload) const;
    void send(const std::vector<std::uint8_t> &payload) const;
    [[nodiscard]] std::optional<std::vector<std::uint8_t>> receive_nowait() const;
    [[nodiscard]] std::optional<std::vector<std::uint8_t>>
    receive(std::chrono::milliseconds timeout) const;

  private:
    explicit IsotpSocket(int descriptor) noexcept;
    [[nodiscard]] static IsotpSocket open_virtual(std::string_view interface_name, Node node,
                                                  bool server);
    int descriptor_{-1};
};

[[nodiscard]] std::vector<std::uint8_t> isotp_request(std::string_view interface_name, Node node,
                                                      const std::vector<std::uint8_t> &request,
                                                      std::chrono::milliseconds timeout);

} // namespace autodiag

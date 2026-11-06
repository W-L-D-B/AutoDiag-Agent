#pragma once

#include "autodiag/protocol.hpp"

#include <chrono>
#include <optional>
#include <string>
#include <string_view>

namespace autodiag {

class SocketCan {
  public:
    SocketCan() = delete;
    ~SocketCan() noexcept;

    SocketCan(const SocketCan &) = delete;
    SocketCan &operator=(const SocketCan &) = delete;
    SocketCan(SocketCan &&other) noexcept;
    SocketCan &operator=(SocketCan &&other) noexcept;

    [[nodiscard]] static SocketCan open_virtual(std::string_view interface_name);
    [[nodiscard]] static bool is_virtual_interface(std::string_view interface_name) noexcept;

    void send(const Frame &frame) const;
    [[nodiscard]] std::optional<Frame> receive() const;
    [[nodiscard]] bool wait_readable(std::chrono::milliseconds timeout) const;

  private:
    explicit SocketCan(int file_descriptor) noexcept;

    int file_descriptor_{-1};
};

} // namespace autodiag

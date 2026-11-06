#include "autodiag/socketcan.hpp"

#include <algorithm>

#include <linux/can.h>
#include <linux/can/raw.h>
#include <net/if.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cctype>
#include <cerrno>
#include <climits>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>

namespace autodiag {
namespace {

constexpr int kArphrdCan{280};

[[nodiscard]] bool has_safe_interface_characters(const std::string_view interface_name) noexcept {
    if (interface_name.empty() || interface_name.size() >= IFNAMSIZ) {
        return false;
    }
    for (const char character : interface_name) {
        const unsigned char value = static_cast<unsigned char>(character);
        if (std::isalnum(value) == 0 && character != '_' && character != '-' && character != '.') {
            return false;
        }
    }
    return true;
}

[[nodiscard]] std::system_error socket_error(const std::string_view context) {
    return {errno, std::generic_category(), std::string{context}};
}

} // namespace

SocketCan::SocketCan(const int file_descriptor) noexcept : file_descriptor_{file_descriptor} {}

SocketCan::~SocketCan() noexcept {
    if (file_descriptor_ >= 0) {
        static_cast<void>(::close(file_descriptor_));
    }
}

SocketCan::SocketCan(SocketCan &&other) noexcept
    : file_descriptor_{std::exchange(other.file_descriptor_, -1)} {}

SocketCan &SocketCan::operator=(SocketCan &&other) noexcept {
    if (this != &other) {
        if (file_descriptor_ >= 0) {
            static_cast<void>(::close(file_descriptor_));
        }
        file_descriptor_ = std::exchange(other.file_descriptor_, -1);
    }
    return *this;
}

bool SocketCan::is_virtual_interface(const std::string_view interface_name) noexcept {
    if (!has_safe_interface_characters(interface_name) || interface_name.rfind("vcan", 0U) != 0U) {
        return false;
    }

    std::error_code error;
    const std::filesystem::path interface_path =
        std::filesystem::path{"/sys/class/net"} / std::string{interface_name};
    const std::filesystem::path resolved_path = std::filesystem::canonical(interface_path, error);
    if (error) {
        return false;
    }
    const std::filesystem::path virtual_root =
        std::filesystem::canonical("/sys/devices/virtual/net", error);
    if (error) {
        return false;
    }
    const std::string resolved = resolved_path.generic_string();
    const std::string root = virtual_root.generic_string() + "/";
    if (resolved.rfind(root, 0U) != 0U) {
        return false;
    }

    std::ifstream type_file{interface_path / "type"};
    int interface_type{};
    if (!(type_file >> interface_type) || interface_type != kArphrdCan) {
        return false;
    }
    return !std::filesystem::exists(interface_path / "device", error) && !error;
}

SocketCan SocketCan::open_virtual(const std::string_view interface_name) {
    if (!is_virtual_interface(interface_name)) {
        throw std::invalid_argument{"refusing non-virtual CAN interface: " +
                                    std::string{interface_name}};
    }

    const int descriptor = ::socket(PF_CAN, SOCK_RAW | SOCK_NONBLOCK | SOCK_CLOEXEC, CAN_RAW);
    if (descriptor < 0) {
        throw socket_error("create SocketCAN socket");
    }
    SocketCan socket{descriptor};

    const int disable_fd_frames{0};
    if (::setsockopt(descriptor, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, &disable_fd_frames,
                     sizeof(disable_fd_frames)) < 0) {
        throw socket_error("disable CAN FD frames");
    }

    const std::string name{interface_name};
    const unsigned int interface_index = ::if_nametoindex(name.c_str());
    if (interface_index == 0U) {
        throw socket_error("resolve CAN interface index");
    }
    sockaddr_can address{};
    address.can_family = AF_CAN;
    address.can_ifindex = static_cast<int>(interface_index);
    if (::bind(descriptor, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) < 0) {
        throw socket_error("bind SocketCAN interface");
    }
    return socket;
}

void SocketCan::send(const Frame &frame) const {
    if (file_descriptor_ < 0) {
        throw std::logic_error{"send on moved-from SocketCAN object"};
    }
    if (frame.dlc > kClassicCanDlc || has_nonstandard_flags(frame.can_id)) {
        throw std::invalid_argument{"refusing invalid classic CAN frame for transmission"};
    }

    can_frame raw{};
    raw.can_id = frame.can_id;
    raw.can_dlc = frame.dlc;
    std::copy(frame.data.begin(), frame.data.end(), raw.data);
    const ssize_t sent = ::write(file_descriptor_, &raw, sizeof(raw));
    if (sent != static_cast<ssize_t>(sizeof(raw))) {
        if (sent < 0) {
            throw socket_error("write SocketCAN frame");
        }
        throw std::runtime_error{"partial SocketCAN frame write"};
    }
}

std::optional<Frame> SocketCan::receive() const {
    if (file_descriptor_ < 0) {
        throw std::logic_error{"receive on moved-from SocketCAN object"};
    }

    can_frame raw{};
    const ssize_t received = ::read(file_descriptor_, &raw, sizeof(raw));
    if (received < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
            return std::nullopt;
        }
        throw socket_error("read SocketCAN frame");
    }
    if (received != static_cast<ssize_t>(sizeof(raw))) {
        throw std::runtime_error{"unexpected SocketCAN read size"};
    }
    Frame frame{};
    frame.can_id = raw.can_id;
    frame.dlc = raw.can_dlc;
    std::copy(raw.data, raw.data + frame.data.size(), frame.data.begin());
    return frame;
}

bool SocketCan::wait_readable(const std::chrono::milliseconds timeout) const {
    if (file_descriptor_ < 0) {
        throw std::logic_error{"wait on moved-from SocketCAN object"};
    }
    const auto bounded_timeout = std::clamp<std::int64_t>(
        timeout.count(), 0, static_cast<std::int64_t>(std::numeric_limits<int>::max()));
    pollfd descriptor{};
    descriptor.fd = file_descriptor_;
    descriptor.events = POLLIN;
    const int result = ::poll(&descriptor, 1U, static_cast<int>(bounded_timeout));
    if (result < 0) {
        if (errno == EINTR) {
            return false;
        }
        throw socket_error("poll SocketCAN frame");
    }
    if ((descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
        throw std::runtime_error{"SocketCAN poll reported an error"};
    }
    return result > 0 && (descriptor.revents & POLLIN) != 0;
}

} // namespace autodiag

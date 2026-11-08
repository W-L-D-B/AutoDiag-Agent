#include "autodiag/isotp.hpp"

#include "autodiag/diagnostics.hpp"
#include "autodiag/socketcan.hpp"

#include <linux/can.h>
#include <linux/can/isotp.h>
#include <net/if.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstddef>
#include <limits>
#include <string>
#include <system_error>
#include <utility>

namespace autodiag {
namespace {

constexpr std::size_t kMaximumPduSize{4095U};
using Clock = std::chrono::steady_clock;
enum class WaitEvent : std::uint8_t { readable = POLLIN };

[[nodiscard]] std::system_error socket_error(const std::string_view context) {
    return {errno, std::generic_category(), std::string{context}};
}

[[nodiscard]] bool wait_until(const int descriptor, const WaitEvent event,
                              const Clock::time_point deadline) {
    while (Clock::now() < deadline) {
        const auto remaining =
            std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
        const auto bounded =
            std::clamp<std::int64_t>(remaining + 1, 0, std::numeric_limits<int>::max());
        pollfd entry{};
        entry.fd = descriptor;
        entry.events = static_cast<short>(event);
        const int result = ::poll(&entry, 1U, static_cast<int>(bounded));
        if (result < 0) {
            if (errno == EINTR) {
                continue;
            }
            throw socket_error("poll ISO-TP socket");
        }
        if ((entry.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
            throw std::system_error{std::make_error_code(std::errc::io_error),
                                    "ISO-TP socket reported an asynchronous error"};
        }
        if ((entry.revents & entry.events) != 0) {
            return true;
        }
    }
    return false;
}

struct Exchange {
    const std::vector<std::uint8_t> &request;
    const std::vector<std::uint8_t> &response;
};

[[nodiscard]] bool matches_response(const Exchange exchange) noexcept {
    const auto &request = exchange.request;
    const auto &response = exchange.response;
    if (response.empty()) {
        return false;
    }
    if (response.front() == 0x7FU) {
        return response.size() == 3U && response[1U] == request.front();
    }
    if (response.front() != static_cast<std::uint8_t>(request.front() + 0x40U)) {
        return false;
    }
    if (request.front() == 0x22U) {
        return request.size() == 3U && response.size() >= 3U && response[1U] == request[1U] &&
               response[2U] == request[2U];
    }
    if (request.front() == 0x10U || request.front() == 0x19U || request.front() == 0x3EU) {
        return request.size() >= 2U && response.size() >= 2U && response[1U] == request[1U];
    }
    return true;
}

} // namespace

IsotpSocket::IsotpSocket(const int descriptor) noexcept : descriptor_{descriptor} {}

IsotpSocket::~IsotpSocket() noexcept {
    if (descriptor_ >= 0) {
        static_cast<void>(::close(descriptor_));
    }
}

IsotpSocket::IsotpSocket(IsotpSocket &&other) noexcept
    : descriptor_{std::exchange(other.descriptor_, -1)} {}

IsotpSocket &IsotpSocket::operator=(IsotpSocket &&other) noexcept {
    if (this != &other) {
        if (descriptor_ >= 0) {
            static_cast<void>(::close(descriptor_));
        }
        descriptor_ = std::exchange(other.descriptor_, -1);
    }
    return *this;
}

IsotpSocket IsotpSocket::open_virtual(const std::string_view interface_name, const Node node,
                                      const bool server) {
    if (!SocketCan::is_virtual_interface(interface_name)) {
        throw std::invalid_argument{"refusing non-virtual ISO-TP interface"};
    }
    const std::uint32_t request_id = diagnostic_request_id(node);
    const std::uint32_t response_id = diagnostic_response_id(node);
    const int descriptor = ::socket(PF_CAN, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, CAN_ISOTP);
    if (descriptor < 0) {
        throw socket_error("create ISO-TP socket");
    }
    IsotpSocket result{descriptor};
    can_isotp_ll_options link{};
    link.mtu = CAN_MTU;
    link.tx_dl = CAN_MAX_DLEN;
    if (::setsockopt(descriptor, SOL_CAN_ISOTP, CAN_ISOTP_LL_OPTS, &link, sizeof(link)) < 0) {
        throw socket_error("configure classic CAN ISO-TP");
    }
    const std::string name{interface_name};
    const unsigned int index = ::if_nametoindex(name.c_str());
    if (index == 0U) {
        throw socket_error("resolve ISO-TP interface");
    }
    sockaddr_can address{};
    address.can_family = AF_CAN;
    address.can_ifindex = static_cast<int>(index);
    address.can_addr.tp.rx_id = server ? request_id : response_id;
    address.can_addr.tp.tx_id = server ? response_id : request_id;
    if (::bind(descriptor, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) < 0) {
        throw socket_error("bind ISO-TP socket");
    }
    return result;
}

IsotpSocket IsotpSocket::open_server(const std::string_view interface_name, const Node node) {
    return open_virtual(interface_name, node, true);
}

IsotpSocket IsotpSocket::open_client(const std::string_view interface_name, const Node node) {
    return open_virtual(interface_name, node, false);
}

bool IsotpSocket::try_send(const std::vector<std::uint8_t> &payload) const {
    if (descriptor_ < 0) {
        throw std::logic_error{"send on moved-from ISO-TP socket"};
    }
    if (payload.empty() || payload.size() > kMaximumPduSize) {
        throw std::invalid_argument{"ISO-TP payload length must be 1..4095"};
    }
    const ssize_t sent = ::send(descriptor_, payload.data(), payload.size(), MSG_DONTWAIT);
    if (sent < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == ENOBUFS || errno == EINTR) {
            return false;
        }
        throw socket_error("send ISO-TP PDU");
    }
    if (sent != static_cast<ssize_t>(payload.size())) {
        throw std::runtime_error{"partial ISO-TP PDU send"};
    }
    return true;
}

void IsotpSocket::send(const std::vector<std::uint8_t> &payload) const {
    if (!try_send(payload)) {
        throw std::system_error{std::make_error_code(std::errc::resource_unavailable_try_again),
                                "ISO-TP transmitter busy"};
    }
}

std::optional<std::vector<std::uint8_t>> IsotpSocket::receive_nowait() const {
    if (descriptor_ < 0) {
        throw std::logic_error{"receive on moved-from ISO-TP socket"};
    }
    std::array<std::uint8_t, kMaximumPduSize + 1U> buffer{};
    const ssize_t received =
        ::recv(descriptor_, buffer.data(), buffer.size(), MSG_DONTWAIT | MSG_TRUNC);
    if (received < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR || errno == ETIMEDOUT ||
            errno == EILSEQ || errno == EBADMSG) {
            // Invalid/incomplete transport PDUs are discarded by the kernel. They must not
            // terminate the periodic ECU publisher or be fabricated into a UDS request.
            return std::nullopt;
        }
        throw socket_error("receive ISO-TP PDU");
    }
    if (received == 0 || received > static_cast<ssize_t>(kMaximumPduSize)) {
        // The kernel supports extended-length PDUs beyond this application contract.
        // recv(MSG_TRUNC) consumed the complete datagram: discard it, keep publishing.
        return std::nullopt;
    }
    return std::vector<std::uint8_t>{buffer.begin(), buffer.begin() + received};
}

std::optional<std::vector<std::uint8_t>>
IsotpSocket::receive(const std::chrono::milliseconds timeout) const {
    if (descriptor_ < 0) {
        throw std::logic_error{"receive on moved-from ISO-TP socket"};
    }
    if (timeout.count() < 0 || timeout > std::chrono::seconds{5}) {
        throw std::invalid_argument{"ISO-TP receive timeout outside 0..5000 ms"};
    }
    const auto deadline = Clock::now() + timeout;
    do {
        if (auto payload = receive_nowait(); payload.has_value()) {
            return payload;
        }
        if (!wait_until(descriptor_, WaitEvent::readable, deadline)) {
            return std::nullopt;
        }
    } while (Clock::now() < deadline);
    return std::nullopt;
}

std::vector<std::uint8_t> isotp_request(const std::string_view interface_name, const Node node,
                                        const std::vector<std::uint8_t> &request,
                                        const std::chrono::milliseconds timeout) {
    if (request.empty() || request.size() > kMaximumPduSize || timeout.count() <= 0 ||
        timeout > std::chrono::seconds{5}) {
        throw std::invalid_argument{"invalid ISO-TP request or deadline"};
    }
    const auto deadline = Clock::now() + timeout;
    IsotpSocket socket = IsotpSocket::open_client(interface_name, node);
    socket.send(request);
    while (Clock::now() < deadline) {
        const auto remaining =
            std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now());
        const auto response = socket.receive(std::max(remaining, std::chrono::milliseconds{0}));
        if (!response.has_value()) {
            break;
        }
        if (matches_response({request, *response})) {
            return *response;
        }
    }
    throw IsotpTimeout{"ISO-TP response deadline exceeded"};
}

} // namespace autodiag

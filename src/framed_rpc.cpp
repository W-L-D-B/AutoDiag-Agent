#include "autodiag/framed_rpc.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <filesystem>
#include <limits>
#include <mutex>
#include <set>
#include <thread>
#include <utility>
#include <vector>

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace autodiag {
namespace {
[[noreturn]] void io_error(std::string_view operation) {
    throw RpcIoError(std::string(operation) + ": " + std::strerror(errno));
}

void wait_for(int descriptor, short event, RpcDeadline deadline, const std::atomic<bool> *stop) {
    for (;;) {
        if (stop != nullptr && stop->load()) {
            throw RpcIoError("RPC server stopping");
        }
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now());
        if (remaining.count() <= 0) {
            throw RpcTimeout("RPC transaction deadline exceeded");
        }
        pollfd item{descriptor, event, 0};
        const auto delay = std::clamp<std::int64_t>(remaining.count(), 1, 50);
        const int result = ::poll(&item, 1, static_cast<int>(delay));
        if (result < 0) {
            if (errno == EINTR) {
                continue;
            }
            io_error("poll RPC socket");
        }
        if ((item.revents & POLLNVAL) != 0) {
            throw RpcIoError("invalid RPC descriptor");
        }
        if (result > 0 && (item.revents & (event | POLLHUP | POLLERR)) != 0) {
            return;
        }
    }
}

bool read_exact(int descriptor, char *data, std::size_t size, RpcDeadline deadline,
                const std::atomic<bool> *stop, bool allow_empty) {
    std::size_t offset{};
    while (offset < size) {
        wait_for(descriptor, POLLIN, deadline, stop);
        const ssize_t count = ::recv(descriptor, data + offset, size - offset, MSG_DONTWAIT);
        if (count == 0) {
            if (allow_empty && offset == 0) {
                return false;
            }
            throw RpcIoError("RPC peer closed an incomplete frame");
        }
        if (count < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
                continue;
            }
            io_error("read RPC socket");
        }
        offset += static_cast<std::size_t>(count);
    }
    return true;
}

void send_exact(int descriptor, std::string_view data, RpcDeadline deadline,
                const std::atomic<bool> *stop) {
    std::size_t offset{};
    while (offset < data.size()) {
        wait_for(descriptor, POLLOUT, deadline, stop);
        const ssize_t count = ::send(descriptor, data.data() + offset, data.size() - offset,
                                     MSG_DONTWAIT | MSG_NOSIGNAL);
        if (count < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
                continue;
            }
            io_error("write RPC socket");
        }
        if (count == 0) {
            throw RpcIoError("zero-byte RPC socket write");
        }
        offset += static_cast<std::size_t>(count);
    }
}

sockaddr_un unix_address(std::string_view path) {
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    if (path.empty() || path.size() >= sizeof(address.sun_path) ||
        path.find('\0') != std::string_view::npos) {
        throw RpcIoError("invalid Unix socket path length");
    }
    std::copy(path.begin(), path.end(), address.sun_path);
    return address;
}

void remove_owned_socket(const std::string &path, std::uint64_t device,
                         std::uint64_t inode) noexcept {
    struct stat status {};
    if (::lstat(path.c_str(), &status) == 0 && S_ISSOCK(status.st_mode) &&
        status.st_uid == ::geteuid() && static_cast<std::uint64_t>(status.st_dev) == device &&
        static_cast<std::uint64_t>(status.st_ino) == inode) {
        static_cast<void>(::unlink(path.c_str()));
    }
}

void serve_client(UniqueFd client, const RpcHandler &handler, const std::atomic<bool> &stop) {
    try {
        for (std::size_t transaction = 0; transaction < 64 && !stop.load(); ++transaction) {
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
            const auto frame = read_rpc_frame(client.get(), deadline, &stop);
            if (!frame.has_value()) {
                return;
            }
            nlohmann::json request;
            nlohmann::json response;
            try {
                request = parse_rpc_json(*frame);
            } catch (const RpcIoError &) {
                response = rpc_error("", {"invalid_request", "invalid JSON object", false, {}});
            }
            if (response.is_null()) {
                try {
                    response = handler(request, deadline);
                } catch (const std::exception &) {
                    response =
                        rpc_error(request_rpc_id(request),
                                  {"internal_error", "request processing failed", false, {}});
                }
            }
            write_rpc_frame(client.get(), response.dump(), deadline, &stop);
        }
    } catch (const std::exception &) {
        // A malformed transport or stopped client only owns this connection.
        return;
    }
}
} // namespace

UniqueFd::UniqueFd(int descriptor) noexcept : descriptor_(descriptor) {}
UniqueFd::~UniqueFd() noexcept {
    if (descriptor_ >= 0) {
        static_cast<void>(::close(descriptor_));
    }
}
UniqueFd::UniqueFd(UniqueFd &&other) noexcept : descriptor_(std::exchange(other.descriptor_, -1)) {}
UniqueFd &UniqueFd::operator=(UniqueFd &&other) noexcept {
    if (this != &other) {
        if (descriptor_ >= 0) {
            static_cast<void>(::close(descriptor_));
        }
        descriptor_ = std::exchange(other.descriptor_, -1);
    }
    return *this;
}
int UniqueFd::get() const noexcept {
    return descriptor_;
}

std::optional<std::string> read_rpc_frame(int descriptor, RpcDeadline deadline,
                                          const std::atomic<bool> *stop) {
    std::array<char, 4> header{};
    if (!read_exact(descriptor, header.data(), header.size(), deadline, stop, true)) {
        return std::nullopt;
    }
    std::uint32_t length{};
    for (char byte : header) {
        length = (length << 8U) | static_cast<unsigned char>(byte);
    }
    if (length == 0 || length > rpc_max_frame_bytes) {
        throw RpcIoError("invalid RPC frame size");
    }
    std::string body(length, '\0');
    static_cast<void>(read_exact(descriptor, body.data(), body.size(), deadline, stop, false));
    return body;
}

void write_rpc_frame(int descriptor, std::string_view payload, RpcDeadline deadline,
                     const std::atomic<bool> *stop) {
    if (payload.empty() || payload.size() > rpc_max_frame_bytes) {
        throw RpcIoError("invalid outbound RPC frame size");
    }
    const auto size = static_cast<std::uint32_t>(payload.size());
    const std::array<char, 4> header{
        static_cast<char>((size >> 24U) & 0xffU), static_cast<char>((size >> 16U) & 0xffU),
        static_cast<char>((size >> 8U) & 0xffU), static_cast<char>(size & 0xffU)};
    send_exact(descriptor, std::string_view(header.data(), header.size()), deadline, stop);
    send_exact(descriptor, payload, deadline, stop);
}

nlohmann::json parse_rpc_json(std::string_view payload) {
    std::vector<std::set<std::string>> keys;
    const auto callback = [&keys](int depth, nlohmann::json::parse_event_t event,
                                  nlohmann::json &value) {
        if (depth > 16) {
            throw RpcIoError("RPC JSON nesting exceeds limit");
        }
        if (event == nlohmann::json::parse_event_t::object_start) {
            keys.emplace_back();
        } else if (event == nlohmann::json::parse_event_t::object_end) {
            keys.pop_back();
        } else if (event == nlohmann::json::parse_event_t::key &&
                   !keys.back().insert(value.get<std::string>()).second) {
            throw RpcIoError("duplicate RPC JSON key");
        }
        return true;
    };
    try {
        auto parsed = nlohmann::json::parse(payload.begin(), payload.end(), callback);
        if (!parsed.is_object()) {
            throw RpcIoError("RPC JSON must be an object");
        }
        return parsed;
    } catch (const nlohmann::json::exception &) {
        throw RpcIoError("invalid RPC JSON encoding");
    }
}

bool valid_rpc_id(std::string_view id) noexcept {
    return !id.empty() && id.size() <= 64 && std::all_of(id.begin(), id.end(), [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
               c == '.' || c == '_' || c == ':' || c == '-';
    });
}
std::string request_rpc_id(const nlohmann::json &request) {
    if (request.is_object() && request.contains("id") && request["id"].is_string()) {
        const auto id = request["id"].get<std::string>();
        if (valid_rpc_id(id)) {
            return id;
        }
    }
    return {};
}
nlohmann::json rpc_error(std::string_view id, const RpcProblem &problem) {
    return {
        {"version", 1},
        {"id", std::string(id)},
        {"ok", false},
        {"result", nullptr},
        {"error",
         {{"code", problem.code},
          {"message", problem.message},
          {"retryable", problem.retryable},
          {"details", problem.details.is_object() ? problem.details : nlohmann::json::object()}}}};
}

UniqueFd connect_unix(std::string_view path, RpcDeadline deadline) {
    const auto address = unix_address(path);
    UniqueFd descriptor(::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
    if (descriptor.get() < 0) {
        io_error("create Unix client");
    }
    if (::connect(descriptor.get(), reinterpret_cast<const sockaddr *>(&address), sizeof(address)) <
        0) {
        if (errno != EINPROGRESS && errno != EAGAIN) {
            io_error("connect Unix service");
        }
        wait_for(descriptor.get(), POLLOUT, deadline, nullptr);
        int error{};
        socklen_t size = sizeof(error);
        if (::getsockopt(descriptor.get(), SOL_SOCKET, SO_ERROR, &error, &size) < 0) {
            io_error("inspect Unix connection");
        }
        if (error != 0) {
            errno = error;
            io_error("connect Unix service");
        }
    }
    return descriptor;
}

UniqueFd create_tcp_listener(std::string_view host, std::uint16_t port) {
    if (host != "127.0.0.1") {
        throw RpcIoError("TCP gateway only accepts loopback binding");
    }
    UniqueFd descriptor(::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
    if (descriptor.get() < 0) {
        io_error("create TCP listener");
    }
    const int enabled = 1;
    if (::setsockopt(descriptor.get(), SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled)) < 0) {
        io_error("configure TCP listener");
    }
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(port);
    if (::bind(descriptor.get(), reinterpret_cast<const sockaddr *>(&address), sizeof(address)) <
            0 ||
        ::listen(descriptor.get(), static_cast<int>(rpc_max_clients)) < 0) {
        io_error("bind TCP listener");
    }
    return descriptor;
}

UnixListener::UnixListener(std::string path) : path_(std::move(path)) {
    const auto requested = std::filesystem::absolute(path_).lexically_normal();
    struct stat parent {};
    if (::lstat(requested.parent_path().c_str(), &parent) < 0) {
        io_error("inspect Unix socket parent");
    }
    if (!S_ISDIR(parent.st_mode) || parent.st_uid != ::geteuid() || (parent.st_mode & 0077) != 0) {
        throw RpcIoError("Unix socket parent must be an owned private directory");
    }
    path_ = (std::filesystem::canonical(requested.parent_path()) / requested.filename()).string();
    const auto address = unix_address(path_);
    struct stat previous {};
    if (::lstat(path_.c_str(), &previous) == 0) {
        if (!S_ISSOCK(previous.st_mode) || previous.st_uid != ::geteuid()) {
            throw RpcIoError("refusing existing non-owned socket path");
        }
        UniqueFd probe(::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
        if (probe.get() < 0) {
            io_error("create socket probe");
        }
        if (::connect(probe.get(), reinterpret_cast<const sockaddr *>(&address), sizeof(address)) ==
                0 ||
            (errno != ECONNREFUSED && errno != ENOENT)) {
            throw RpcIoError("Unix service is active or cannot be proved stale");
        }
        remove_owned_socket(path_, static_cast<std::uint64_t>(previous.st_dev),
                            static_cast<std::uint64_t>(previous.st_ino));
    } else if (errno != ENOENT) {
        io_error("inspect Unix socket path");
    }
    descriptor_ = UniqueFd(::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
    if (descriptor_.get() < 0) {
        io_error("create Unix listener");
    }
    if (::bind(descriptor_.get(), reinterpret_cast<const sockaddr *>(&address), sizeof(address)) <
        0) {
        io_error("bind Unix listener");
    }
    struct stat current {};
    if (::lstat(path_.c_str(), &current) < 0) {
        io_error("inspect bound Unix socket");
    }
    device_ = static_cast<std::uint64_t>(current.st_dev);
    inode_ = static_cast<std::uint64_t>(current.st_ino);
    owns_path_ = true;
    if (::chmod(path_.c_str(), 0600) < 0 ||
        ::listen(descriptor_.get(), static_cast<int>(rpc_max_clients)) < 0) {
        const int saved = errno;
        remove_owned_socket(path_, device_, inode_);
        owns_path_ = false;
        errno = saved;
        io_error("configure Unix listener");
    }
}
UnixListener::~UnixListener() noexcept {
    if (owns_path_) {
        remove_owned_socket(path_, device_, inode_);
    }
}
int UnixListener::get() const noexcept {
    return descriptor_.get();
}

void run_rpc_server(int listener, const RpcHandler &handler) {
    sigset_t signals;
    ::sigemptyset(&signals);
    ::sigaddset(&signals, SIGINT);
    ::sigaddset(&signals, SIGTERM);
    if (::pthread_sigmask(SIG_BLOCK, &signals, nullptr) != 0) {
        throw RpcIoError("cannot block termination signals");
    }
    UniqueFd signal_fd(::signalfd(-1, &signals, SFD_NONBLOCK | SFD_CLOEXEC));
    if (signal_fd.get() < 0) {
        io_error("create termination descriptor");
    }
    std::atomic<bool> stopping{false};
    std::mutex mutex;
    std::condition_variable ready;
    std::deque<UniqueFd> pending;
    std::size_t active{};
    std::vector<std::thread> workers;
    const auto finish = [&] {
        stopping.store(true);
        {
            const std::lock_guard<std::mutex> lock(mutex);
            pending.clear();
        }
        ready.notify_all();
        for (auto &worker : workers) {
            if (worker.joinable()) {
                worker.join();
            }
        }
    };
    try {
        for (std::size_t i = 0; i < rpc_max_clients; ++i) {
            workers.emplace_back([&] {
                for (;;) {
                    UniqueFd client;
                    {
                        std::unique_lock<std::mutex> lock(mutex);
                        ready.wait(lock, [&] { return stopping.load() || !pending.empty(); });
                        if (stopping.load()) {
                            return;
                        }
                        client = std::move(pending.front());
                        pending.pop_front();
                    }
                    serve_client(std::move(client), handler, stopping);
                    {
                        const std::lock_guard<std::mutex> lock(mutex);
                        --active;
                    }
                }
            });
        }
        while (!stopping.load()) {
            std::array<pollfd, 2> items{{{listener, POLLIN, 0}, {signal_fd.get(), POLLIN, 0}}};
            const int status = ::poll(items.data(), items.size(), 100);
            if (status < 0) {
                if (errno == EINTR) {
                    continue;
                }
                io_error("poll RPC listener");
            }
            if ((items[1].revents & POLLIN) != 0) {
                signalfd_siginfo info{};
                static_cast<void>(::read(signal_fd.get(), &info, sizeof(info)));
                break;
            }
            if ((items[0].revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
                throw RpcIoError("RPC listener failed");
            }
            if ((items[0].revents & POLLIN) == 0) {
                continue;
            }
            for (std::size_t count = 0; count < rpc_max_clients; ++count) {
                UniqueFd client(
                    ::accept4(listener, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC));
                if (client.get() < 0) {
                    if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
                        break;
                    }
                    io_error("accept RPC client");
                }
                {
                    const std::lock_guard<std::mutex> lock(mutex);
                    if (active >= rpc_max_clients) {
                        continue;
                    }
                    ++active;
                    pending.push_back(std::move(client));
                }
                ready.notify_one();
            }
        }
    } catch (...) {
        finish();
        throw;
    }
    finish();
}

nlohmann::json forward_rpc(std::string_view path, const nlohmann::json &request,
                           RpcDeadline deadline) {
    const auto id = request_rpc_id(request);
    bool complete_response = false;
    try {
        auto connection = connect_unix(path, deadline);
        write_rpc_frame(connection.get(), request.dump(), deadline);
        const auto body = read_rpc_frame(connection.get(), deadline);
        if (!body.has_value()) {
            throw RpcIoError("empty service response");
        }
        complete_response = true;
        auto response = parse_rpc_json(*body);
        const std::set<std::string> expected{"version", "id", "ok", "result", "error"};
        std::set<std::string> fields;
        for (auto item = response.begin(); item != response.end(); ++item) {
            fields.insert(item.key());
        }
        if (fields != expected || !response["version"].is_number_integer() ||
            response["version"] != 1 || !response["id"].is_string() || response["id"] != id ||
            !response["ok"].is_boolean()) {
            throw RpcIoError("invalid service response envelope");
        }
        if (response["ok"].get<bool>()) {
            if (!response["result"].is_object() || !response["error"].is_null()) {
                throw RpcIoError("invalid service success response");
            }
        } else {
            const auto &error = response["error"];
            if (!response["result"].is_null() || !error.is_object() || error.size() != 4 ||
                !error.contains("code") || !error["code"].is_string() ||
                !error.contains("message") || !error["message"].is_string() ||
                !error.contains("retryable") || !error["retryable"].is_boolean() ||
                !error.contains("details") || !error["details"].is_object()) {
                throw RpcIoError("invalid service error response");
            }
        }
        return response;
    } catch (const RpcTimeout &) {
        return rpc_error(id, {"unavailable", "local service transaction timed out", true, {}});
    } catch (const std::exception &) {
        return rpc_error(
            id, {complete_response ? "internal_error" : "unavailable",
                 complete_response ? "invalid local service response" : "local service unavailable",
                 !complete_response,
                 {}});
    }
}
} // namespace autodiag

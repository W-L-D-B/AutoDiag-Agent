#pragma once

#include <atomic>
#include <chrono>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

namespace autodiag {

constexpr std::size_t rpc_max_frame_bytes{65536U};
constexpr std::size_t rpc_max_clients{8U};
using RpcDeadline = std::chrono::steady_clock::time_point;

class RpcIoError : public std::runtime_error {
  public:
    using std::runtime_error::runtime_error;
};
class RpcTimeout : public RpcIoError {
  public:
    using RpcIoError::RpcIoError;
};

class UniqueFd {
  public:
    explicit UniqueFd(int descriptor = -1) noexcept;
    ~UniqueFd() noexcept;
    UniqueFd(const UniqueFd &) = delete;
    UniqueFd &operator=(const UniqueFd &) = delete;
    UniqueFd(UniqueFd &&other) noexcept;
    UniqueFd &operator=(UniqueFd &&other) noexcept;
    [[nodiscard]] int get() const noexcept;

  private:
    int descriptor_;
};

struct RpcProblem {
    std::string code;
    std::string message;
    bool retryable{false};
    nlohmann::json details = nlohmann::json::object();
};

[[nodiscard]] std::optional<std::string> read_rpc_frame(int descriptor, RpcDeadline deadline,
                                                        const std::atomic<bool> *stop = nullptr);
void write_rpc_frame(int descriptor, std::string_view payload, RpcDeadline deadline,
                     const std::atomic<bool> *stop = nullptr);
[[nodiscard]] nlohmann::json parse_rpc_json(std::string_view payload);
[[nodiscard]] bool valid_rpc_id(std::string_view id) noexcept;
[[nodiscard]] std::string request_rpc_id(const nlohmann::json &request);
[[nodiscard]] nlohmann::json rpc_error(std::string_view id, const RpcProblem &problem);
[[nodiscard]] UniqueFd connect_unix(std::string_view path, RpcDeadline deadline);
[[nodiscard]] UniqueFd create_tcp_listener(std::string_view host, std::uint16_t port);

class UnixListener {
  public:
    explicit UnixListener(std::string path);
    ~UnixListener() noexcept;
    UnixListener(const UnixListener &) = delete;
    UnixListener &operator=(const UnixListener &) = delete;
    UnixListener(UnixListener &&) = delete;
    UnixListener &operator=(UnixListener &&) = delete;
    [[nodiscard]] int get() const noexcept;

  private:
    std::string path_;
    UniqueFd descriptor_;
    std::uint64_t device_{0};
    std::uint64_t inode_{0};
    bool owns_path_{false};
};

using RpcHandler = std::function<nlohmann::json(const nlohmann::json &, RpcDeadline)>;
// The caller retains the listener until all workers have joined on return.
void run_rpc_server(int listener, const RpcHandler &handler);
[[nodiscard]] nlohmann::json forward_rpc(std::string_view path, const nlohmann::json &request,
                                         RpcDeadline deadline);

} // namespace autodiag

#include "autodiag/cli.hpp"
#include "autodiag/protocol.hpp"
#include "autodiag/socketcan.hpp"
#include "autodiag/state.hpp"

#include <nlohmann/json.hpp>

#include <chrono>
#include <csignal>
#include <cstdint>
#include <exception>
#include <fcntl.h>
#include <filesystem>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

#include <sys/stat.h>
#include <unistd.h>

namespace {

volatile std::sig_atomic_t g_stop_requested{0};

extern "C" void request_stop(int) {
    g_stop_requested = 1;
}

[[nodiscard]] std::uint64_t elapsed_ms(const std::chrono::steady_clock::time_point start) {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
                                          std::chrono::steady_clock::now() - start)
                                          .count());
}

[[nodiscard]] nlohmann::json nullable_node(const std::optional<autodiag::Node> &node) {
    return node.has_value() ? nlohmann::json(std::string{autodiag::node_name(*node)})
                            : nlohmann::json(nullptr);
}

[[nodiscard]] nlohmann::json nullable_message(const autodiag::MessageType message) {
    return message == autodiag::MessageType::unknown
               ? nlohmann::json(nullptr)
               : nlohmann::json(std::string{autodiag::message_name(message)});
}

[[nodiscard]] nlohmann::json frame_to_json(const autodiag::Frame &frame,
                                           const autodiag::FrameDecision &decision,
                                           const std::uint64_t elapsed) {
    return {{"schema_version", 1U},
            {"type", "frame"},
            {"elapsed_ms", elapsed},
            {"raw_id", frame.can_id},
            {"id", autodiag::arbitration_id(frame.can_id)},
            {"dlc", frame.dlc},
            {"data", autodiag::data_to_upper_hex(frame)},
            {"accepted", decision.accepted},
            {"reason", autodiag::reject_reason_name(decision.reason)},
            {"node", nullable_node(decision.node)},
            {"message", nullable_message(decision.message)},
            {"counter", decision.counter.has_value() ? nlohmann::json(*decision.counter)
                                                     : nlohmann::json(nullptr)}};
}

[[nodiscard]] std::system_error posix_error(const std::string_view context) {
    return {errno, std::generic_category(), std::string{context}};
}

class SafeOutputFile {
  public:
    explicit SafeOutputFile(const int descriptor) noexcept : descriptor_{descriptor} {}
    ~SafeOutputFile() noexcept {
        if (descriptor_ >= 0) {
            static_cast<void>(::close(descriptor_));
        }
    }

    SafeOutputFile(const SafeOutputFile &) = delete;
    SafeOutputFile &operator=(const SafeOutputFile &) = delete;
    SafeOutputFile(SafeOutputFile &&other) noexcept
        : descriptor_{std::exchange(other.descriptor_, -1)} {}
    SafeOutputFile &operator=(SafeOutputFile &&other) noexcept {
        if (this != &other) {
            if (descriptor_ >= 0) {
                static_cast<void>(::close(descriptor_));
            }
            descriptor_ = std::exchange(other.descriptor_, -1);
        }
        return *this;
    }

    [[nodiscard]] static SafeOutputFile create_new(const std::filesystem::path &path) {
        const int descriptor = ::open(
            path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, S_IRUSR | S_IWUSR);
        if (descriptor < 0) {
            throw posix_error("create output file");
        }
        return SafeOutputFile{descriptor};
    }

    void write_all(const std::string_view contents) const {
        const char *cursor = contents.data();
        std::size_t remaining = contents.size();
        while (remaining > 0U) {
            const ssize_t written = ::write(descriptor_, cursor, remaining);
            if (written < 0) {
                if (errno == EINTR) {
                    continue;
                }
                throw posix_error("write output file");
            }
            if (written == 0) {
                throw std::runtime_error{"zero-byte write to output file"};
            }
            const std::size_t count = static_cast<std::size_t>(written);
            cursor += count;
            remaining -= count;
        }
    }

    void synchronize() const {
        while (::fsync(descriptor_) < 0) {
            if (errno != EINTR) {
                throw posix_error("synchronize output file");
            }
        }
    }

    void close_checked() {
        const int descriptor = std::exchange(descriptor_, -1);
        if (descriptor >= 0 && ::close(descriptor) < 0) {
            throw posix_error("close output file");
        }
    }

  private:
    int descriptor_{-1};
};

[[nodiscard]] std::filesystem::path normalize_new_output_path(const std::string &raw_path,
                                                              const std::string_view label) {
    if (raw_path.empty()) {
        throw std::invalid_argument{std::string{label} + " path is empty"};
    }
    const std::filesystem::path requested = std::filesystem::absolute(raw_path).lexically_normal();
    const std::filesystem::path parent = requested.parent_path();
    if (requested.filename().empty() || parent.empty()) {
        throw std::invalid_argument{std::string{label} + " must name a file"};
    }

    struct stat parent_stat {};
    if (::lstat(parent.c_str(), &parent_stat) < 0) {
        throw posix_error("inspect output directory");
    }
    if (!S_ISDIR(parent_stat.st_mode) || S_ISLNK(parent_stat.st_mode) ||
        parent_stat.st_uid != ::geteuid() || (parent_stat.st_mode & (S_IWGRP | S_IWOTH)) != 0U) {
        throw std::invalid_argument{std::string{label} +
                                    " parent must be an owned, non-symlink, private directory"};
    }

    std::error_code error;
    const std::filesystem::path canonical_parent = std::filesystem::canonical(parent, error);
    if (error) {
        throw std::system_error{error, "canonicalize output directory"};
    }
    const std::filesystem::path result = canonical_parent / requested.filename();
    struct stat existing {};
    if (::lstat(result.c_str(), &existing) == 0) {
        throw std::invalid_argument{std::string{label} +
                                    " already exists; choose a new evidence path"};
    }
    if (errno != ENOENT) {
        throw posix_error("inspect output path");
    }
    return result;
}

void assert_replaceable_snapshot(const std::filesystem::path &destination) {
    struct stat existing {};
    if (::lstat(destination.c_str(), &existing) < 0) {
        if (errno == ENOENT) {
            return;
        }
        throw posix_error("inspect existing snapshot");
    }
    if (!S_ISREG(existing.st_mode) || S_ISLNK(existing.st_mode) || existing.st_nlink != 1 ||
        existing.st_uid != ::geteuid()) {
        throw std::invalid_argument{"refusing unsafe existing snapshot path"};
    }
}

void write_json_line(SafeOutputFile &log, const nlohmann::json &value) {
    const std::string row = value.dump() + "\n";
    log.write_all(row);
    log.synchronize();
}

void atomic_write_json(const std::filesystem::path &destination, const nlohmann::json &value) {
    assert_replaceable_snapshot(destination);
    std::string template_path =
        (destination.parent_path() / ("." + destination.filename().string() + ".autodiag-XXXXXX"))
            .string();
    std::vector<char> template_buffer(template_path.begin(), template_path.end());
    template_buffer.push_back('\0');
    const int descriptor = ::mkstemp(template_buffer.data());
    if (descriptor < 0) {
        throw posix_error("create temporary snapshot");
    }
    SafeOutputFile output{descriptor};
    try {
        output.write_all(value.dump(2) + "\n");
        output.synchronize();
        output.close_checked();
        if (::rename(template_buffer.data(), destination.c_str()) < 0) {
            throw posix_error("atomically replace snapshot");
        }
    } catch (...) {
        static_cast<void>(::unlink(template_buffer.data()));
        throw;
    }
}

[[nodiscard]] nlohmann::json snapshot_to_json(const autodiag::MonitorSnapshot &snapshot,
                                              const std::string_view capture_state,
                                              const bool ready) {
    nlohmann::json output = snapshot;
    output["schema_version"] = 1U;
    output["capture_state"] = std::string{capture_state};
    output["ready"] = ready;
    return output;
}

int run(const autodiag::MonitorOptions &options) {
    autodiag::SocketCan socket = autodiag::SocketCan::open_virtual(options.interface_name);
    const std::filesystem::path snapshot_path =
        normalize_new_output_path(options.snapshot_path, "snapshot");
    const std::filesystem::path log_path = normalize_new_output_path(options.log_path, "log");
    if (snapshot_path == log_path) {
        throw std::invalid_argument{"snapshot and log paths must differ"};
    }
    SafeOutputFile log = SafeOutputFile::create_new(log_path);

    const auto start = std::chrono::steady_clock::now();
    autodiag::MonitorState state;
    atomic_write_json(snapshot_path, snapshot_to_json(state.snapshot(0U), "listening", true));
    write_json_line(log, {{"schema_version", 1U}, {"type", "capture_start"}, {"elapsed_ms", 0U}});

    while (g_stop_requested == 0 && elapsed_ms(start) < options.duration_ms) {
        static_cast<void>(socket.wait_readable(std::chrono::milliseconds{25}));
        while (g_stop_requested == 0 && elapsed_ms(start) < options.duration_ms) {
            const std::optional<autodiag::Frame> frame = socket.receive();
            if (!frame.has_value()) {
                break;
            }
            const std::uint64_t frame_elapsed = elapsed_ms(start);
            const autodiag::FrameDecision decision = state.ingest(*frame, frame_elapsed);
            write_json_line(log, frame_to_json(*frame, decision, frame_elapsed));
        }
    }

    const std::uint64_t completed_at = elapsed_ms(start);
    write_json_line(
        log, {{"schema_version", 1U}, {"type", "capture_end"}, {"elapsed_ms", completed_at}});
    atomic_write_json(snapshot_path,
                      snapshot_to_json(state.snapshot(completed_at), "completed", false));
    return 0;
}

} // namespace

int main(int argc, char *argv[]) {
    std::signal(SIGINT, request_stop);
    std::signal(SIGTERM, request_stop);
    try {
        return run(autodiag::parse_monitor_options(argc, argv));
    } catch (const autodiag::UsageError &error) {
        std::cerr << "usage error: " << error.what() << '\n';
        return 2;
    } catch (const std::exception &error) {
        std::cerr << "autodiag-monitor: " << error.what() << '\n';
        return 1;
    }
}

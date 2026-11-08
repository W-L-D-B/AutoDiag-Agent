#include "autodiag/framed_rpc.hpp"

#include <charconv>
#include <cstdint>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>

int main(int argc, char *argv[]) {
    std::string socket_path;
    std::string host{"127.0.0.1"};
    std::uint16_t port{18770};
    try {
        std::set<std::string> seen;
        for (int i = 1; i < argc; i += 2) {
            const std::string option(argv[i]);
            if (i + 1 >= argc || !seen.insert(option).second) {
                throw std::invalid_argument("missing or duplicate gateway argument");
            }
            const std::string value(argv[i + 1]);
            if (option == "--socket") {
                socket_path = value;
            } else if (option == "--host") {
                host = value;
            } else if (option == "--port") {
                unsigned int parsed{};
                const auto result =
                    std::from_chars(value.data(), value.data() + value.size(), parsed);
                if (result.ec != std::errc{} || result.ptr != value.data() + value.size() ||
                    parsed == 0 || parsed > 65535) {
                    throw std::invalid_argument("port must be 1..65535");
                }
                port = static_cast<std::uint16_t>(parsed);
            } else {
                throw std::invalid_argument("unknown gateway argument");
            }
        }
        if (socket_path.empty() || host != "127.0.0.1") {
            throw std::invalid_argument("--socket PATH and loopback host are required");
        }
    } catch (const std::invalid_argument &error) {
        std::cerr << "usage error: " << error.what() << '\n';
        return 2;
    }
    try {
        auto listener = autodiag::create_tcp_listener(host, port);
        std::cout << "AutoDiag TCP gateway listening\n" << std::flush;
        autodiag::run_rpc_server(listener.get(), [&socket_path](const nlohmann::json &request,
                                                                autodiag::RpcDeadline deadline) {
            return autodiag::forward_rpc(socket_path, request, deadline);
        });
    } catch (const std::exception &error) {
        std::cerr << "autodiag-gateway: " << error.what() << '\n';
        return 1;
    }
    return 0;
}

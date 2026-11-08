#include "autodiag/framed_rpc.hpp"
#include "autodiag/service.hpp"

#include <csignal>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>

#include <pthread.h>

int main(int argc, char *argv[]) {
    std::string interface_name{"vcan0"};
    std::string socket_path;
    try {
        std::set<std::string> seen;
        for (int i = 1; i < argc; i += 2) {
            const std::string option(argv[i]);
            if (i + 1 >= argc || !seen.insert(option).second) {
                throw std::invalid_argument("missing or duplicate service argument");
            }
            if (option == "--interface") {
                interface_name = argv[i + 1];
            } else if (option == "--socket") {
                socket_path = argv[i + 1];
            } else {
                throw std::invalid_argument("unknown service argument");
            }
        }
        if (socket_path.empty()) {
            throw std::invalid_argument("--socket PATH is required");
        }
    } catch (const std::invalid_argument &error) {
        std::cerr << "usage error: " << error.what() << '\n';
        return 2;
    }
    try {
        // All subsequently created collector/worker threads inherit the blocked mask.
        sigset_t signals;
        ::sigemptyset(&signals);
        ::sigaddset(&signals, SIGINT);
        ::sigaddset(&signals, SIGTERM);
        if (::pthread_sigmask(SIG_BLOCK, &signals, nullptr) != 0) {
            throw std::runtime_error("cannot configure service termination signals");
        }
        autodiag::UnixListener listener(socket_path);
        autodiag::LiveService service(interface_name);
        service.start();
        std::cout << "AutoDiag Unix service listening\n" << std::flush;
        autodiag::run_rpc_server(listener.get(), [&service](const nlohmann::json &request,
                                                            autodiag::RpcDeadline deadline) {
            return service.dispatch(request, deadline);
        });
    } catch (const std::exception &error) {
        std::cerr << "autodiag-service: " << error.what() << '\n';
        return 1;
    }
    return 0;
}

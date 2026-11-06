#include <linux/can.h>
#include <linux/can/isotp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <iostream>

int main() {
    struct SocketCheck {
        const char *name;
        int family;
        int type;
        int protocol;
    };
    const SocketCheck checks[] = {
        {"Unix Domain Socket", AF_UNIX, SOCK_STREAM, 0},
        {"TCP", AF_INET, SOCK_STREAM, 0},
        {"SocketCAN RAW", AF_CAN, SOCK_RAW, CAN_RAW},
        {"SocketCAN ISO-TP", AF_CAN, SOCK_DGRAM, CAN_ISOTP},
    };
    bool passed = true;
    for (const auto &check : checks) {
        const int fd = ::socket(check.family, check.type | SOCK_CLOEXEC, check.protocol);
        if (fd < 0) {
            std::cerr << check.name << ": FAIL: " << std::strerror(errno) << '\n';
            passed = false;
        } else {
            ::close(fd);
            std::cout << check.name << ": OK\n";
        }
    }
    return passed ? 0 : 1;
}

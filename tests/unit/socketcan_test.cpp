#include "autodiag/socketcan.hpp"

#include <gtest/gtest.h>

namespace autodiag {
namespace {

TEST(socketcan_test, rejects_nonvirtual_interfaces) {
    EXPECT_FALSE(SocketCan::is_virtual_interface("lo"));
    EXPECT_FALSE(SocketCan::is_virtual_interface("can0"));
    EXPECT_FALSE(SocketCan::is_virtual_interface("vcan0/unsafe"));
}

} // namespace
} // namespace autodiag

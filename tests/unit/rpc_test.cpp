#include "autodiag/framed_rpc.hpp"

#include <array>
#include <chrono>
#include <future>
#include <string>
#include <sys/socket.h>

#include <gtest/gtest.h>

namespace autodiag {
namespace {
using namespace std::chrono_literals;

TEST(rpc_test, reads_fragmented_and_concatenated_frames) {
    std::array<int, 2> descriptors{};
    ASSERT_EQ(::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, descriptors.data()), 0);
    const UniqueFd receiver{descriptors[0]};
    const UniqueFd sender{descriptors[1]};
    const auto deadline = std::chrono::steady_clock::now() + 2s;
    const std::array<unsigned char, 7> first{0, 0, 0, 3, 'o', 'n', 'e'};
    ASSERT_EQ(::send(sender.get(), first.data(), 2, MSG_NOSIGNAL), 2);
    auto pending =
        std::async(std::launch::async, [&] { return read_rpc_frame(receiver.get(), deadline); });
    ASSERT_EQ(::send(sender.get(), first.data() + 2, first.size() - 2, MSG_NOSIGNAL), 5);
    write_rpc_frame(sender.get(), "two", deadline);
    ASSERT_EQ(pending.get(), "one");
    ASSERT_EQ(read_rpc_frame(receiver.get(), deadline), "two");
}

TEST(rpc_test, rejects_zero_and_oversized_lengths) {
    for (const std::array<unsigned char, 4> header :
         {std::array<unsigned char, 4>{0, 0, 0, 0}, {0, 1, 0, 1}}) {
        std::array<int, 2> descriptors{};
        ASSERT_EQ(::socketpair(AF_UNIX, SOCK_STREAM, 0, descriptors.data()), 0);
        const UniqueFd receiver{descriptors[0]};
        const UniqueFd sender{descriptors[1]};
        ASSERT_EQ(::send(sender.get(), header.data(), header.size(), MSG_NOSIGNAL), 4);
        EXPECT_THROW(read_rpc_frame(receiver.get(), std::chrono::steady_clock::now() + 1s),
                     RpcIoError);
    }
}

TEST(rpc_test, deadline_covers_incomplete_body) {
    std::array<int, 2> descriptors{};
    ASSERT_EQ(::socketpair(AF_UNIX, SOCK_STREAM, 0, descriptors.data()), 0);
    const UniqueFd receiver{descriptors[0]};
    const UniqueFd sender{descriptors[1]};
    const std::array<unsigned char, 5> header{0, 0, 0, 2, 'x'};
    ASSERT_EQ(::send(sender.get(), header.data(), header.size(), MSG_NOSIGNAL), 5);
    EXPECT_THROW(read_rpc_frame(receiver.get(), std::chrono::steady_clock::now() + 20ms),
                 RpcTimeout);
}

TEST(rpc_test, parses_only_strict_json_objects) {
    EXPECT_EQ(parse_rpc_json(R"({"id":"one","params":{}})")["id"], "one");
    EXPECT_THROW(parse_rpc_json(R"({"a":1,"a":2})"), RpcIoError);
    EXPECT_THROW(parse_rpc_json(R"({"params":{"a":1,"a":2}})"), RpcIoError);
    EXPECT_THROW(parse_rpc_json("[]"), RpcIoError);
    EXPECT_THROW(parse_rpc_json(R"({"n":NaN})"), RpcIoError);
    EXPECT_THROW(parse_rpc_json(std::string{"{\"x\":\""} + '\xFF' + "\"}"), RpcIoError);
}

TEST(rpc_test, identifiers_and_error_envelopes_are_typed) {
    EXPECT_TRUE(valid_rpc_id("req-01.a:b_c"));
    EXPECT_FALSE(valid_rpc_id(""));
    EXPECT_FALSE(valid_rpc_id("../path/"));
    EXPECT_FALSE(valid_rpc_id(std::string(65, 'x')));
    const auto result = rpc_error("one", {"unavailable", "service unavailable", true, {}});
    EXPECT_EQ(result["id"], "one");
    EXPECT_FALSE(result["ok"].get<bool>());
    EXPECT_TRUE(result["result"].is_null());
    EXPECT_TRUE(result["error"]["details"].is_object());
}
} // namespace
} // namespace autodiag

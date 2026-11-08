#include "autodiag/diagnostics.hpp"

#include <stdexcept>
#include <string_view>

namespace autodiag {
namespace {

using Bytes = std::vector<std::uint8_t>;

[[nodiscard]] Bytes negative(const std::uint8_t sid, const std::uint8_t code) {
    return {0x7FU, sid, code};
}

[[nodiscard]] bool is_signal_did(const Node node, const std::uint16_t did) noexcept {
    switch (node) {
    case Node::bms:
        return did >= 0xD100U && did <= 0xD102U;
    case Node::hvac:
        return did >= 0xD200U && did <= 0xD202U;
    case Node::body:
        return did >= 0xD300U && did <= 0xD302U;
    }
    return false;
}

[[nodiscard]] bool has_clean_sample(const Node node, const Frame &sample) noexcept {
    const DecodeResult decoded = decode_frame(sample);
    return decoded.reason == RejectReason::accepted && decoded.node == node &&
           decoded.message != MessageType::heartbeat;
}

void append_signal(Bytes &response, const std::uint16_t did, const Frame &sample) {
    const std::uint8_t selector = static_cast<std::uint8_t>(did & 0xFFU);
    if (did >= 0xD100U && did <= 0xD102U) {
        const std::size_t offset = static_cast<std::size_t>(selector) * 2U;
        response.insert(response.end(), sample.data.begin() + static_cast<std::ptrdiff_t>(offset),
                        sample.data.begin() + static_cast<std::ptrdiff_t>(offset + 2U));
    } else if (did == 0xD200U) {
        response.insert(response.end(), sample.data.begin(), sample.data.begin() + 2);
        response.push_back(static_cast<std::uint8_t>(sample.data[5U] & 1U));
    } else if (did == 0xD201U) {
        response.insert(response.end(), sample.data.begin() + 2, sample.data.begin() + 4);
    } else if (did == 0xD202U) {
        response.push_back(sample.data[4U]);
        response.push_back(static_cast<std::uint8_t>(sample.data[5U] & 1U));
    } else {
        const std::size_t offset = static_cast<std::size_t>(selector) * 2U;
        response.insert(response.end(), sample.data.begin() + static_cast<std::ptrdiff_t>(offset),
                        sample.data.begin() + static_cast<std::ptrdiff_t>(offset + 2U));
    }
}

[[nodiscard]] std::optional<std::uint32_t> current_dtc(const Node node, const Frame &sample) {
    const DecodeResult decoded = decode_frame(sample);
    if (node == Node::bms) {
        const auto *status = std::get_if<BmsStatus>(&decoded.payload);
        if (status != nullptr && status->temperature_deci_celsius > 900) {
            return 0xA10001U;
        }
    } else if (node == Node::hvac) {
        const auto *status = std::get_if<HvacStatus>(&decoded.payload);
        if (status != nullptr && !status->cabin_valid) {
            return 0xA20001U;
        }
    }
    return std::nullopt;
}

} // namespace

std::uint32_t diagnostic_request_id(const Node node) {
    switch (node) {
    case Node::bms:
        return 0x7E0U;
    case Node::hvac:
        return 0x7E1U;
    case Node::body:
        return 0x7E2U;
    }
    throw std::invalid_argument{"unknown diagnostic node"};
}

std::uint32_t diagnostic_response_id(const Node node) {
    return diagnostic_request_id(node) + 8U;
}

UdsServer::UdsServer(const Node node) : node_{node} {
    static_cast<void>(diagnostic_request_id(node));
}

std::uint8_t UdsServer::session() const noexcept {
    return session_;
}

std::vector<std::uint8_t> UdsServer::handle(const Bytes &request, const std::uint64_t now_ms,
                                            const Frame &clean_sample) {
    if (last_valid_request_ms_.has_value() &&
        (now_ms < *last_valid_request_ms_ ||
         now_ms - *last_valid_request_ms_ >= kDiagnosticSessionTimeoutMs)) {
        session_ = 0x01U;
    }
    if (request.empty()) {
        return negative(0x00U, 0x13U);
    }
    const std::uint8_t sid = request.front();
    Bytes response;
    switch (sid) {
    case 0x10U:
        if (request.size() != 2U) {
            return negative(sid, 0x13U);
        }
        if (request[1U] != 0x01U && request[1U] != 0x03U) {
            return negative(sid, 0x12U);
        }
        session_ = request[1U];
        response = {0x50U, session_, 0x00U, 0xFAU, 0x00U, 0xC8U};
        break;
    case 0x22U: {
        if (request.size() != 3U) {
            return negative(sid, 0x13U);
        }
        const std::uint16_t did = static_cast<std::uint16_t>(
            (static_cast<std::uint16_t>(request[1U]) << 8U) | request[2U]);
        response = {0x62U, request[1U], request[2U]};
        if (did == 0xF180U || did == 0xF187U) {
            const std::string_view text = did == 0xF180U ? "AutoDiag/1.0" : node_name(node_);
            response.insert(response.end(), text.begin(), text.end());
        } else {
            if (!is_signal_did(node_, did) || session_ != 0x03U) {
                return negative(sid, 0x31U);
            }
            if (!has_clean_sample(node_, clean_sample)) {
                return negative(sid, 0x22U);
            }
            append_signal(response, did, clean_sample);
        }
        break;
    }
    case 0x19U: {
        if (request.size() != 3U) {
            return negative(sid, 0x13U);
        }
        if (request[1U] != 0x02U) {
            return negative(sid, 0x12U);
        }
        if (!has_clean_sample(node_, clean_sample)) {
            return negative(sid, 0x22U);
        }
        response = {0x59U, 0x02U, 0x09U};
        const std::optional<std::uint32_t> dtc = current_dtc(node_, clean_sample);
        if (dtc.has_value() && (request[2U] & 0x09U) != 0U) {
            response.push_back(static_cast<std::uint8_t>((*dtc >> 16U) & 0xFFU));
            response.push_back(static_cast<std::uint8_t>((*dtc >> 8U) & 0xFFU));
            response.push_back(static_cast<std::uint8_t>(*dtc & 0xFFU));
            response.push_back(0x09U);
        }
        break;
    }
    case 0x3EU:
        if (request.size() != 2U) {
            return negative(sid, 0x13U);
        }
        if (request[1U] != 0x00U) {
            return negative(sid, 0x12U);
        }
        response = {0x7EU, 0x00U};
        break;
    default:
        return negative(sid, 0x11U);
    }
    last_valid_request_ms_ = now_ms;
    return response;
}

} // namespace autodiag

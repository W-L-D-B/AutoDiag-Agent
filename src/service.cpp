#include "autodiag/service.hpp"

#include "autodiag/isotp.hpp"
#include "autodiag/socketcan.hpp"

#include <algorithm>
#include <chrono>
#include <iomanip>
#include <set>
#include <sstream>
#include <stdexcept>
#include <utility>
#include <vector>

#include <unistd.h>

namespace autodiag {
namespace {
using Json = nlohmann::json;
using Bytes = std::vector<std::uint8_t>;

class InvalidParams : public std::invalid_argument {
  public:
    using std::invalid_argument::invalid_argument;
};
class ServiceFailure : public std::runtime_error {
  public:
    explicit ServiceFailure(RpcProblem value)
        : std::runtime_error(value.message), problem(std::move(value)) {}
    RpcProblem problem;
};

std::string new_boot_id() {
    return "boot-" + std::to_string(::getpid()) + "-" +
           std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());
}

std::uint64_t integer(const Json &value, std::uint64_t maximum) {
    if (!value.is_number_integer() ||
        (!value.is_number_unsigned() && value.get<std::int64_t>() < 0)) {
        throw InvalidParams("expected a non-negative integer");
    }
    const auto result = value.get<std::uint64_t>();
    if (result > maximum) {
        throw InvalidParams("integer parameter is outside its range");
    }
    return result;
}

struct FieldSet {
    std::initializer_list<std::string_view> required{};
    std::initializer_list<std::string_view> optional{};
};
void fields(const Json &params, const FieldSet specification) {
    if (!params.is_object()) {
        throw InvalidParams("params must be an object");
    }
    std::set<std::string> allowed;
    for (const auto name : specification.required) {
        allowed.insert(std::string(name));
        if (!params.contains(name)) {
            throw InvalidParams("required parameter missing");
        }
    }
    for (const auto name : specification.optional) {
        allowed.insert(std::string(name));
    }
    for (auto item = params.begin(); item != params.end(); ++item) {
        if (allowed.count(item.key()) == 0) {
            throw InvalidParams("unknown parameter");
        }
    }
}

Node node_parameter(const Json &params) {
    const auto &name = params.at("node");
    if (!name.is_string()) {
        throw InvalidParams("node must be a known name");
    }
    if (name == "bms") {
        return Node::bms;
    }
    if (name == "hvac") {
        return Node::hvac;
    }
    if (name == "body") {
        return Node::body;
    }
    throw InvalidParams("unknown ECU node");
}

std::string text_parameter(const Json &params, std::string_view key) {
    const auto &value = params.at(key);
    if (!value.is_string()) {
        throw InvalidParams("expected a string parameter");
    }
    return value.get<std::string>();
}

std::size_t index_of(Node node) {
    return static_cast<std::size_t>(node) - 1U;
}
std::string hex(const Bytes &bytes) {
    std::ostringstream stream;
    stream << std::hex << std::uppercase << std::setfill('0');
    for (const auto byte : bytes) {
        stream << std::setw(2) << static_cast<unsigned int>(byte);
    }
    return stream.str();
}
std::string dtc_hex(std::uint32_t code) {
    return hex({static_cast<std::uint8_t>((code >> 16U) & 0xffU),
                static_cast<std::uint8_t>((code >> 8U) & 0xffU),
                static_cast<std::uint8_t>(code & 0xffU)});
}
std::uint16_t raw_unsigned(const Bytes &bytes, std::size_t at = 0) {
    return static_cast<std::uint16_t>(static_cast<std::uint16_t>(bytes.at(at)) |
                                      (static_cast<std::uint16_t>(bytes.at(at + 1)) << 8U));
}
std::int32_t raw_signed(const Bytes &bytes) {
    const std::uint16_t value = raw_unsigned(bytes);
    return value <= 32767U ? static_cast<std::int32_t>(value)
                           : static_cast<std::int32_t>(value) - 65536;
}
void require_payload(bool condition) {
    if (!condition) {
        throw ServiceFailure({"internal_error", "invalid ECU diagnostic payload", false, {}});
    }
}

const std::map<Node, std::map<std::string, std::string>> signal_units{
    {Node::bms, {{"voltage_v", "V"}, {"current_a", "A"}, {"temperature_c", "degC"}}},
    {Node::hvac,
     {{"cabin_temperature_c", "degC"},
      {"target_temperature_c", "degC"},
      {"fan_level", ""},
      {"cabin_sensor_valid", ""}}},
    {Node::body,
     {{"speed_kph", "km/h"}, {"door_mask", ""}, {"light_mask", ""}, {"supply_voltage_v", "V"}}}};

struct DidValues {
    Json values = Json::object();
    Json units = Json::object();
    bool valid{true};
};

DidValues decode_did(Node node, std::uint16_t did, const Bytes &data) {
    DidValues output;
    const auto scalar = [&](std::string_view name, double value, std::string_view unit) {
        output.values[std::string(name)] = value;
        output.units[std::string(name)] = std::string(unit);
    };
    const auto discrete = [&](std::string_view name, const Json &value) {
        output.values[std::string(name)] = value;
        output.units[std::string(name)] = "";
    };
    if (did == 0xF180U || did == 0xF187U) {
        require_payload(!data.empty() && data.size() <= 64 &&
                        std::all_of(data.begin(), data.end(),
                                    [](std::uint8_t c) { return c >= 32 && c <= 126; }));
        const std::string value(data.begin(), data.end());
        if (did == 0xF187U) {
            require_payload(value == node_name(node));
        }
        discrete(did == 0xF180U ? "software_version" : "node_name", value);
    } else if (node == Node::bms && did >= 0xD100U && did <= 0xD102U) {
        require_payload(data.size() == 2);
        if (did == 0xD100U) {
            const auto value = raw_unsigned(data);
            require_payload(value <= 8000);
            scalar("voltage_v", value * 0.1, "V");
        } else if (did == 0xD101U) {
            const auto value = raw_signed(data);
            require_payload(value >= -2000 && value <= 2000);
            scalar("current_a", value * 0.1, "A");
        } else {
            const auto value = raw_signed(data);
            require_payload(value >= -400 && value <= 1500);
            scalar("temperature_c", value * 0.1, "degC");
        }
    } else if (node == Node::hvac && did >= 0xD200U && did <= 0xD202U) {
        if (did == 0xD200U) {
            require_payload(data.size() == 3);
            const auto value = raw_signed(data);
            require_payload(value >= -400 && value <= 1250 && data[2] <= 1);
            scalar("cabin_temperature_c", value * 0.1, "degC");
            discrete("cabin_sensor_valid", data[2] == 1);
            output.valid = data[2] == 1;
        } else if (did == 0xD201U) {
            require_payload(data.size() == 2);
            const auto value = raw_signed(data);
            require_payload(value >= 160 && value <= 320);
            scalar("target_temperature_c", value * 0.1, "degC");
        } else {
            require_payload(data.size() == 2 && data[0] <= 7 && data[1] <= 1);
            discrete("fan_level", data[0]);
            discrete("cabin_sensor_valid", data[1] == 1);
            output.valid = data[1] == 1;
        }
    } else if (node == Node::body && did >= 0xD300U && did <= 0xD302U) {
        require_payload(data.size() == 2);
        if (did == 0xD300U) {
            const auto value = raw_unsigned(data);
            require_payload(value <= 20000);
            scalar("speed_kph", value * 0.01, "km/h");
        } else if (did == 0xD301U) {
            require_payload(data[0] <= 31 && data[1] <= 7);
            discrete("door_mask", data[0]);
            discrete("light_mask", data[1]);
        } else {
            const auto value = raw_unsigned(data);
            require_payload(value <= 2500);
            scalar("supply_voltage_v", value * 0.01, "V");
        }
    } else {
        require_payload(false);
    }
    return output;
}
} // namespace

LiveService::LiveService(std::string interface_name, Clock clock)
    : interface_name_(std::move(interface_name)), boot_id_(new_boot_id()),
      clock_(std::move(clock)) {
    if (!clock_) {
        const auto start = std::chrono::steady_clock::now();
        clock_ = [start] {
            return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
                                                  std::chrono::steady_clock::now() - start)
                                                  .count());
        };
    }
}
LiveService::~LiveService() noexcept {
    stop();
}
std::string LiveService::evidence(std::string_view kind) {
    return boot_id_ + ":" + std::string(kind) + ":" +
           std::to_string(evidence_sequence_.fetch_add(1));
}

void LiveService::start() {
    if (collector_.joinable()) {
        throw std::logic_error("collector already started");
    }
    auto socket = SocketCan::open_virtual(interface_name_);
    stopping_.store(false);
    collector_ = std::thread([this, socket = std::move(socket)]() mutable {
        collector_running_.store(true);
        try {
            while (!stopping_.load()) {
                static_cast<void>(socket.wait_readable(std::chrono::milliseconds(20)));
                while (!stopping_.load()) {
                    auto frame = socket.receive();
                    if (!frame.has_value()) {
                        break;
                    }
                    ingest(*frame, clock_());
                }
            }
        } catch (const std::exception &) {
            // Health exposes the failed collector without fabricating new samples.
            collector_running_.store(false);
            return;
        }
        collector_running_.store(false);
    });
}
void LiveService::stop() noexcept {
    stopping_.store(true);
    if (collector_.joinable()) {
        collector_.join();
    }
}

void LiveService::ingest(const Frame &frame, std::uint64_t now_ms) {
    if (message_type_for_arbitration_id(arbitration_id(frame.can_id)) == MessageType::unknown) {
        return;
    }
    const std::lock_guard<std::mutex> lock(state_mutex_);
    const auto decision = state_.ingest(frame, now_ms);
    if (!decision.accepted || decision.message == MessageType::heartbeat ||
        !decision.node.has_value()) {
        return;
    }
    const Json state = state_.snapshot(now_ms);
    auto &history = history_[index_of(*decision.node)];
    history.samples.push_back({now_ms,
                               state["nodes"][std::string(node_name(*decision.node))]["signals"],
                               evidence("sample")});
    if (history.samples.size() > 512) {
        history.discarded_through = history.samples.front().at_ms;
        history.samples.pop_front();
    }
}
Json LiveService::snapshot(std::uint64_t now_ms) {
    const std::lock_guard<std::mutex> lock(state_mutex_);
    return state_.snapshot(now_ms);
}

Bytes LiveService::exchange(Node node, const Bytes &request, std::string_view phase,
                            RpcDeadline deadline) {
    Json details{{"node", std::string(node_name(node))},
                 {"sid", request.at(0)},
                 {"phase", std::string(phase)},
                 {"request_hex", hex(request)},
                 {"evidence_id", evidence("uds")}};
    auto &quarantined = tester_quarantined_[index_of(node)];
    if (quarantined) {
        throw ServiceFailure({"uds_quarantined",
                              "diagnostic channel uncertain after timeout; restart ECU and service",
                              false, details});
    }
    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
        deadline - std::chrono::steady_clock::now());
    if (remaining.count() <= 0) {
        throw ServiceFailure(
            {"uds_timeout", "diagnostic transaction deadline exceeded", true, details});
    }
    try {
        const auto response = isotp_request(interface_name_, node, request,
                                            std::min(remaining, std::chrono::milliseconds(250)));
        details["response_hex"] = hex(response);
        if (response.size() == 3 && response[0] == 0x7F) {
            details["nrc"] = response[2];
            throw ServiceFailure({"uds_negative_response",
                                  "ECU returned a negative diagnostic response", false, details});
        }
        return response;
    } catch (const IsotpTimeout &) {
        quarantined = true;
        throw ServiceFailure(
            {"uds_timeout", "ECU did not respond; diagnostic channel quarantined", false, details});
    } catch (const std::system_error &) {
        quarantined = true;
        throw ServiceFailure(
            {"unavailable", "diagnostic transport failed; channel quarantined", false, details});
    }
}

Json LiveService::read_did(Node node, std::uint16_t did, RpcDeadline deadline) {
    std::unique_lock<std::timed_mutex> tester(tester_mutexes_[index_of(node)], std::defer_lock);
    if (!tester.try_lock_until(deadline)) {
        throw ServiceFailure({"uds_timeout", "diagnostic tester busy", true, {}});
    }
    if (did != 0xF180U && did != 0xF187U) {
        const auto session = exchange(node, {0x10, 0x03}, "session", deadline);
        require_payload(session.size() == 6 && session[0] == 0x50 && session[1] == 0x03);
    }
    const auto response = exchange(
        node, {0x22, static_cast<std::uint8_t>(did >> 8U), static_cast<std::uint8_t>(did & 0xffU)},
        "request", deadline);
    require_payload(response.size() >= 3 && response[0] == 0x62 && response[1] == (did >> 8U) &&
                    response[2] == (did & 0xffU));
    const Bytes payload(response.begin() + 3, response.end());
    const auto decoded = decode_did(node, did, payload);
    return {{"node", std::string(node_name(node))},
            {"did", did},
            {"values", decoded.values},
            {"units", decoded.units},
            {"valid", decoded.valid},
            {"raw_hex", hex(payload)},
            {"response_hex", hex(response)}};
}

Json LiveService::read_dtcs(Node node, std::uint8_t mask, RpcDeadline deadline) {
    std::unique_lock<std::timed_mutex> tester(tester_mutexes_[index_of(node)], std::defer_lock);
    if (!tester.try_lock_until(deadline)) {
        throw ServiceFailure({"uds_timeout", "diagnostic tester busy", true, {}});
    }
    const auto response = exchange(node, {0x19, 0x02, mask}, "request", deadline);
    require_payload(response.size() >= 3 && response[0] == 0x59 && response[1] == 0x02 &&
                    response[2] == 0x09 && (response.size() - 3) % 4 == 0);
    auto records = Json::array();
    for (std::size_t i = 3; i < response.size(); i += 4) {
        const auto code = (static_cast<std::uint32_t>(response[i]) << 16U) |
                          (static_cast<std::uint32_t>(response[i + 1]) << 8U) | response[i + 2];
        const auto status = response[i + 3];
        require_payload((status & ~0x09U) == 0 && (status & mask) != 0);
        require_payload((node == Node::bms && code == 0xA10001U) ||
                        (node == Node::hvac && code == 0xA20001U));
        records.push_back(
            {{"code", dtc_hex(code)},
             {"status", status},
             {"description", code == 0xA10001U ? "BMS temperature above 90 C"
                                               : "HVAC cabin temperature input invalid"}});
    }
    return {{"node", std::string(node_name(node))},
            {"status_mask", mask},
            {"dtcs", records},
            {"response_hex", hex(response)}};
}

Json LiveService::execute(std::string_view method, const Json &params, RpcDeadline deadline) {
    if (method == "health") {
        fields(params, {});
        return {{"service", "autodiag-service"},
                {"boot_id", boot_id_},
                {"uptime_ms", clock_()},
                {"collector_running", collector_running_.load()},
                {"interface", interface_name_}};
    }
    if (method == "snapshot") {
        fields(params, {});
        return snapshot(clock_());
    }
    if (method == "list_ecus") {
        fields(params, {});
        const auto data = snapshot(clock_());
        auto ecus = Json::array();
        for (const Node node : {Node::bms, Node::hvac, Node::body}) {
            const auto &state = data["nodes"][std::string(node_name(node))];
            ecus.push_back({{"node", std::string(node_name(node))},
                            {"node_id", static_cast<unsigned int>(node)},
                            {"online", state["online"]},
                            {"data_status", state["data_status"]},
                            {"capabilities", {"read_did", "read_dtcs", "signal_window"}}});
        }
        return {{"ecus", ecus}};
    }
    if (method == "read_did") {
        fields(params, {{"node", "did"}});
        return read_did(node_parameter(params),
                        static_cast<std::uint16_t>(integer(params["did"], 65535)), deadline);
    }
    if (method == "read_dtcs") {
        fields(params, {{"node"}, {"status_mask"}});
        return read_dtcs(node_parameter(params),
                         static_cast<std::uint8_t>(params.contains("status_mask")
                                                       ? integer(params["status_mask"], 255)
                                                       : 255),
                         deadline);
    }
    if (method == "signal_window") {
        fields(params, {{"node", "signal", "window_ms"}, {"max_samples"}});
        const auto node = node_parameter(params);
        const auto signal = text_parameter(params, "signal");
        const auto &units = signal_units.at(node);
        if (units.count(signal) == 0) {
            throw InvalidParams("unknown signal for node");
        }
        const auto window = integer(params["window_ms"], 60000);
        const auto maximum =
            params.contains("max_samples") ? integer(params["max_samples"], 512) : 128;
        if (window == 0 || maximum == 0) {
            throw InvalidParams("window and max_samples must be positive");
        }
        const auto now = clock_();
        const auto start = now > window ? now - window : 0;
        const std::lock_guard<std::mutex> lock(state_mutex_);
        const auto &history = history_[index_of(node)];
        std::vector<const Sample *> matches;
        for (const auto &sample : history.samples) {
            if (sample.at_ms >= start && sample.at_ms <= now) {
                matches.push_back(&sample);
            }
        }
        auto samples = Json::array();
        const std::size_t skip = matches.size() > maximum ? matches.size() - maximum : 0;
        for (std::size_t i = skip; i < matches.size(); ++i) {
            const auto &sample = *matches[i];
            samples.push_back({{"t_ms", sample.at_ms},
                               {"value", sample.signals.at(signal)},
                               {"evidence_id", sample.evidence_id}});
        }
        return {
            {"node", std::string(node_name(node))},
            {"signal", signal},
            {"unit", units.at(signal)},
            {"samples", samples},
            {"sample_count", matches.size()},
            {"returned_count", samples.size()},
            {"truncated", skip != 0 || (history.discarded_through.has_value() &&
                                        *history.discarded_through >= start)},
            {"oldest_available_ms", matches.empty() ? Json(nullptr) : Json(matches.front()->at_ms)},
            {"latest_ms", matches.empty() ? Json(nullptr) : Json(matches.back()->at_ms)}};
    }
    if (method == "probe") {
        fields(params, {{"node", "kind"}});
        const auto node = node_parameter(params);
        const auto kind = text_parameter(params, "kind");
        Json checks = Json::object();
        bool passed = true;
        if (kind == "identity") {
            checks["software"] = read_did(node, 0xF180U, deadline);
            checks["identity"] = read_did(node, 0xF187U, deadline);
        } else if (kind == "diagnostic") {
            const auto did = node == Node::bms ? 0xD102U : node == Node::hvac ? 0xD200U : 0xD300U;
            checks["signal"] = read_did(node, static_cast<std::uint16_t>(did), deadline);
            checks["dtcs"] = read_dtcs(node, 255, deadline);
        } else if (kind == "freshness") {
            const auto data = snapshot(clock_());
            checks = data["nodes"][std::string(node_name(node))];
            passed = checks["online"].get<bool>() && checks["data_status"] == "fresh";
        } else {
            throw InvalidParams("unknown probe kind");
        }
        return {{"node", std::string(node_name(node))},
                {"kind", kind},
                {"passed", passed},
                {"checks", checks}};
    }
    if (method == "contract") {
        fields(params, {});
        return {{"protocol", "autodiag-service-v1"},
                {"methods",
                 {"health", "snapshot", "list_ecus", "read_did", "read_dtcs", "signal_window",
                  "probe", "contract"}},
                {"limits",
                 {{"max_frame_bytes", 65536},
                  {"max_clients", 8},
                  {"history_per_node", 512},
                  {"max_window_ms", 60000},
                  {"transaction_timeout_ms", 2000},
                  {"uds_timeout_ms", 250}}}};
    }
    throw ServiceFailure({"method_not_found", "unknown RPC method", false, {}});
}

Json LiveService::dispatch(const Json &request, RpcDeadline deadline) {
    const auto id = request_rpc_id(request);
    if (!request.is_object() || request.size() != 4 || !request.contains("version") ||
        !request["version"].is_number_integer() || request["version"] != 1 || id.empty() ||
        !request.contains("method") || !request["method"].is_string() ||
        !request.contains("params") || !request["params"].is_object()) {
        return rpc_error(id,
                         {"invalid_request", "invalid version, ID or request envelope", false, {}});
    }
    try {
        auto result = execute(request["method"].get<std::string>(), request["params"], deadline);
        result["evidence_id"] = evidence("rpc");
        result["observed_at_ms"] = clock_();
        return {{"version", 1}, {"id", id}, {"ok", true}, {"result", result}, {"error", nullptr}};
    } catch (const InvalidParams &error) {
        return rpc_error(id, {"invalid_params", error.what(), false, {}});
    } catch (const ServiceFailure &error) {
        return rpc_error(id, error.problem);
    } catch (const std::exception &) {
        return rpc_error(id, {"internal_error", "service operation failed", false, {}});
    }
}
} // namespace autodiag

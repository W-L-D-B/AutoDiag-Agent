cmake_minimum_required(VERSION 3.22)
project(AutoDiagAgent VERSION 0.1.0 LANGUAGES CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "Build the Linux components inside AutoDiagUbuntu (WSL2).")
endif()

option(AUTODIAG_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer" OFF)
option(AUTODIAG_COVERAGE "Instrument project code for gcovr" OFF)
if(AUTODIAG_SANITIZERS AND AUTODIAG_COVERAGE)
    message(FATAL_ERROR "Use separate sanitizer and coverage builds.")
endif()

find_package(Threads REQUIRED)
find_package(nlohmann_json 3.10 REQUIRED)
add_library(autodiag_options INTERFACE)
target_compile_features(autodiag_options INTERFACE cxx_std_17)
target_compile_options(autodiag_options INTERFACE -Wall -Wextra -Wpedantic -Werror)
if(AUTODIAG_SANITIZERS)
    target_compile_options(autodiag_options INTERFACE -fsanitize=address,undefined -fno-omit-frame-pointer)
    target_link_options(autodiag_options INTERFACE -fsanitize=address,undefined)
endif()
if(AUTODIAG_COVERAGE)
    target_compile_options(autodiag_options INTERFACE --coverage -O0 -g)
    target_link_options(autodiag_options INTERFACE --coverage)
endif()

add_library(autodiag_core
    src/protocol.cpp
    src/lin.cpp
    src/state.cpp
    src/socketcan.cpp
    src/cli.cpp
    src/diagnostics.cpp
    src/isotp.cpp
    src/framed_rpc.cpp
    src/service.cpp
)
target_include_directories(autodiag_core PUBLIC include)
target_link_libraries(autodiag_core PUBLIC autodiag_options nlohmann_json::nlohmann_json Threads::Threads)

add_executable(autodiag-ecu apps/ecu_main.cpp)
target_link_libraries(autodiag-ecu PRIVATE autodiag_core)
add_executable(autodiag-monitor apps/monitor_main.cpp)
target_link_libraries(autodiag-monitor PRIVATE autodiag_core)
add_executable(autodiag-service apps/service_main.cpp)
target_link_libraries(autodiag-service PRIVATE autodiag_core)
add_executable(autodiag-gateway apps/gateway_main.cpp)
target_link_libraries(autodiag-gateway PRIVATE autodiag_core)

add_executable(autodiag-env-check tools/cpp_env_check.cpp)
target_link_libraries(autodiag-env-check PRIVATE autodiag_options)

include(CTest)
if(BUILD_TESTING)
    find_package(GTest REQUIRED)
    find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
    add_executable(autodiag_unit_tests
        tests/unit/protocol_test.cpp
        tests/unit/lin_test.cpp
        tests/unit/state_test.cpp
        tests/unit/socketcan_test.cpp
        tests/unit/cli_test.cpp
        tests/unit/diagnostics_test.cpp
        tests/unit/rpc_test.cpp
        tests/unit/service_test.cpp
    )
    target_link_libraries(autodiag_unit_tests PRIVATE autodiag_core GTest::gtest_main)
    include(GoogleTest)
    gtest_discover_tests(autodiag_unit_tests DISCOVERY_MODE PRE_TEST
        PROPERTIES LABELS unit)
    add_test(NAME linux_socket_environment COMMAND autodiag-env-check)
    set_tests_properties(linux_socket_environment PROPERTIES LABELS environment)
    add_test(NAME python_contract
        COMMAND "${Python3_EXECUTABLE}" -m pytest -q tests/contract tests/unit_python
            "--junitxml=${CMAKE_BINARY_DIR}/python-contract.xml")
    add_test(NAME virtual_bench_integration
        COMMAND "${Python3_EXECUTABLE}" -m pytest -q tests/integration
            "--junitxml=${CMAKE_BINARY_DIR}/python-integration.xml")
    add_test(NAME python_agent
        COMMAND "${Python3_EXECUTABLE}" -m pytest -q tests/agent
            "--junitxml=${CMAKE_BINARY_DIR}/python-agent.xml")
    set_tests_properties(python_contract virtual_bench_integration python_agent PROPERTIES
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
        ENVIRONMENT "AUTODIAG_BUILD_DIR=$<TARGET_FILE_DIR:autodiag-ecu>"
        TIMEOUT 180
        RUN_SERIAL TRUE)
    set_tests_properties(python_contract PROPERTIES LABELS contract)
    set_tests_properties(virtual_bench_integration PROPERTIES LABELS integration)
    set_tests_properties(python_agent PROPERTIES LABELS agent)
endif()

cmake_minimum_required(VERSION 3.22)
project(AutoDiagAgent VERSION 0.0.0 LANGUAGES CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "Build the Linux components inside AutoDiagUbuntu (WSL2).")
endif()

add_executable(autodiag-env-check tools/cpp_env_check.cpp)
target_compile_features(autodiag-env-check PRIVATE cxx_std_17)
target_compile_options(autodiag-env-check PRIVATE -Wall -Wextra -Wpedantic)

enable_testing()
add_test(NAME linux_socket_environment COMMAND autodiag-env-check)

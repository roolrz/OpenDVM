#!/usr/bin/env bash
set -euo pipefail

g++ -std=c++17 -O2 -Wall -o /usr/local/bin/mmdvm-status mmdvm_status.cpp -lmariadbclient

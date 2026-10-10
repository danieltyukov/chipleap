// Stimulus for the simulator-neutral SystemVerilog testbench (tb_chipleap.sv).
//
// Reads the same test cases as tb_chipleap.cpp from stdin ("m k n fmt seed stall_pct
// latency"), generates the same random activations and packed weights, plans and starts
// each job with the generated driver while recording its register writes, computes the
// golden result, and writes everything a simulator needs as hexadecimal numbers only, so
// that every simulator's $fscanf reads it the same way. Per case:
//
//   m k n fmt stall latency seed tm tn tk x_resident n_writes n_words n_expect y_addr
//   offset value            n_writes lines: register writes, in driver order
//   address value           n_words lines: the 32-bit words of X and W in memory
//   address value           n_expect lines: the expected output words
//
// Xcelium, VCS, Questa and Verilator all run the same file, so the hardware is checked
// the same way whichever simulator runs it.
//
// SPDX-License-Identifier: MIT

#include <cstdint>
#include <cstdio>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

static std::vector<std::pair<uint32_t, uint32_t>> g_writes;
static void rec_write(uint32_t off, uint32_t val) { g_writes.push_back({off, val}); }

#define CHIPLEAP_WRITE(base, off, val) rec_write((off), (val))
#define CHIPLEAP_READ(base, off) 0u
#include "chipleap_driver.h"

static int8_t wval(const std::vector<uint8_t> &w, uint32_t row_bytes, uint32_t r, uint32_t e,
                   uint32_t fmt) {
  uint32_t bitpos = e * fmt;
  uint8_t byte = w[r * row_bytes + bitpos / 8];
  uint32_t v = (byte >> (bitpos % 8)) & ((1u << fmt) - 1u);
  if (v & (1u << (fmt - 1))) v |= ~((1u << fmt) - 1u);
  return static_cast<int8_t>(v);
}

static uint32_t word_at(const std::vector<uint8_t> &b, size_t i) {
  uint32_t v = 0;
  for (int j = 3; j >= 0; j--) v = (v << 8) | (i + j < b.size() ? b[i + j] : 0u);
  return v;
}

int main() {
  std::string line;
  int failures = 0;
  while (std::getline(std::cin, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::istringstream in(line);
    uint32_t m, k, n, fmt, seed;
    int stall_pct, latency;
    in >> m >> k >> n >> fmt >> seed >> stall_pct >> latency;
    if (latency < 1) latency = 1;
    std::mt19937 rng(seed);

    chipleap_plan_t plan;
    if (chipleap_plan(m, k, n, fmt, &plan) != 0) {
      std::fprintf(stderr, "case %u %u %u %u: no tile plan\n", m, k, n, fmt);
      failures++;
      continue;
    }
    // Same data as tb_chipleap.cpp for the same seed.
    uint32_t kp = plan.k_pad;
    uint32_t w_row = kp * fmt / 8;
    std::vector<uint8_t> x(static_cast<size_t>(m) * kp, 0), w(static_cast<size_t>(n) * w_row, 0);
    for (uint32_t i = 0; i < m; i++)
      for (uint32_t j = 0; j < k; j++) x[i * kp + j] = static_cast<uint8_t>(rng());
    for (uint32_t r = 0; r < n; r++)
      for (uint32_t e = 0; e < k; e++) {
        uint32_t bitpos = e * fmt;
        uint32_t v = rng() & ((1u << fmt) - 1u);
        w[r * w_row + bitpos / 8] |= static_cast<uint8_t>(v << (bitpos % 8));
      }
    const uint32_t X = 0x1000;
    const uint32_t W = (X + static_cast<uint32_t>(x.size()) + 0xFFF) & ~0xFFFu;
    const uint32_t Y = (W + static_cast<uint32_t>(w.size()) + 0xFFF) & ~0xFFFu;

    g_writes.clear();
    chipleap_start(0, X, W, Y, m, n, fmt, &plan);

    std::vector<std::pair<uint32_t, uint32_t>> words;
    for (size_t i = 0; i < x.size(); i += 4) words.push_back({X + static_cast<uint32_t>(i), word_at(x, i)});
    for (size_t i = 0; i < w.size(); i += 4) words.push_back({W + static_cast<uint32_t>(i), word_at(w, i)});

    std::vector<std::pair<uint32_t, uint32_t>> expect;
    for (uint32_t i = 0; i < m; i++)
      for (uint32_t j = 0; j < n; j++) {
        int32_t ref = 0;
        for (uint32_t e = 0; e < k; e++)
          ref += static_cast<int32_t>(static_cast<int8_t>(x[i * kp + e])) * wval(w, w_row, j, e, fmt);
        expect.push_back({Y + (i * n + j) * 4, static_cast<uint32_t>(ref)});
      }

    std::printf("%x %x %x %x %x %x %x %x %x %x %x %x %zx %zx %x\n", m, k, n, fmt, stall_pct,
                latency, seed, plan.tm, plan.tn, plan.tk, plan.x_resident,
                static_cast<unsigned>(g_writes.size()), words.size(), expect.size(), Y);
    for (auto &p : g_writes) std::printf("%x %x\n", p.first, p.second);
    for (auto &p : words) std::printf("%x %x\n", p.first, p.second);
    for (auto &p : expect) std::printf("%x %x\n", p.first, p.second);
  }
  return failures ? 1 : 0;
}

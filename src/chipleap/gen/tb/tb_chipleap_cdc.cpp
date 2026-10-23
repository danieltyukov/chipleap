// Verilator testbench for the ChipLeap accelerator in its own clock domain
// (chipleap_accel_cdc).
//
// The same cases as tb_chipleap.cpp ("m k n fmt seed stall_pct latency" per line on
// stdin), with two clocks: the bus clock, on which the register interface and the OBI
// memory model run, and the accelerator's clock. Their periods and the accelerator
// clock's first edge are given in picoseconds as "bus_ps accel_ps accel_phase_ps" on the
// command line. The memory model's latency counts bus cycles. Each result line has the
// job's cycle count as the accelerator counts it (its own clock) and the bus cycles the
// job took.
//
// SPDX-License-Identifier: MIT

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "Vchipleap_accel_cdc.h"
#include "verilated.h"

static Vchipleap_accel_cdc *g_top = nullptr;
static uint64_t g_cycle = 0;  // bus cycles
static uint64_t g_acycle = 0;  // accelerator cycles
static uint64_t bus_ps = 10000, acc_ps = 10000, t_bus = 0, t_acc = 0;
static void reg_write(uint32_t off, uint32_t val);
static uint32_t reg_read(uint32_t off);

#define CHIPLEAP_WRITE(base, off, val) reg_write((off), (val))
#define CHIPLEAP_READ(base, off) reg_read((off))
#include "chipleap_driver.h"

template <typename T> static uint32_t word_of(const T &v, int i) {
  return static_cast<uint32_t>(static_cast<uint64_t>(v) >> (32 * i));
}
template <std::size_t N> static uint32_t word_of(const VlWide<N> &v, int i) { return v[i]; }
template <typename T> static void set_word(T &v, int i, uint32_t w) {
  uint64_t x = static_cast<uint64_t>(v);
  x &= ~(0xFFFFFFFFull << (32 * i));
  x |= static_cast<uint64_t>(w) << (32 * i);
  v = static_cast<T>(x);
}
template <std::size_t N> static void set_word(VlWide<N> &v, int i, uint32_t w) { v[i] = w; }
template <typename T> static bool bit_of(const T &v, int i) { return (v >> i) & 1u; }
template <typename T> static void set_bit(T &v, int i, bool b) {
  if (b) v |= static_cast<T>(1ull << i);
  else v &= static_cast<T>(~(1ull << i));
}

struct Pending {
  uint64_t ready;
  uint32_t addr;
};

static std::vector<uint8_t> mem;
static std::deque<Pending> rq[CHIPLEAP_PORTS];
static std::deque<uint64_t> wq;
static std::mt19937 rng;
static int stall_pct = 0;
static int latency = 1;

static uint32_t mem_rd(uint32_t a) {
  return mem[a] | (mem[a + 1] << 8) | (mem[a + 2] << 16) | (static_cast<uint32_t>(mem[a + 3]) << 24);
}
static void mem_wr(uint32_t a, uint32_t v) {
  for (int i = 0; i < 4; i++) mem[a + i] = (v >> (8 * i)) & 0xFF;
}

static bool grant() { return stall_pct == 0 || static_cast<int>(rng() % 100) >= stall_pct; }

// The next rising edge of either clock, or of both when they coincide. On a bus edge the
// memory model presents its grants and responses for the request state before the edge,
// as tb_chipleap.cpp's tick() does.
static void edge() {
  Vchipleap_accel_cdc *t = g_top;
  const uint64_t now = std::min(t_bus, t_acc);
  const bool b = t_bus == now, a = t_acc == now;
  t->clk_i = 0;
  t->accel_clk_i = 0;
  t->eval();
  bool take_r[CHIPLEAP_PORTS] = {};
  bool wg = false, wrv = false;
  uint32_t waddr = 0, wdata = 0, raddr[CHIPLEAP_PORTS] = {};
  if (b) {
    for (int p = 0; p < static_cast<int>(CHIPLEAP_PORTS); p++) {
      bool req = bit_of(t->rd_req_o, p);
      bool g = req && grant();
      set_bit(t->rd_gnt_i, p, g);
      take_r[p] = g;
      bool rv = !rq[p].empty() && rq[p].front().ready <= g_cycle;
      set_bit(t->rd_rvalid_i, p, rv);
      if (rv) set_word(t->rd_rdata_i, p, mem_rd(rq[p].front().addr));
      raddr[p] = word_of(t->rd_addr_o, p);
    }
    wg = t->wr_req_o && grant();
    t->wr_gnt_i = wg;
    wrv = !wq.empty() && wq.front() <= g_cycle;
    t->wr_rvalid_i = wrv;
    waddr = t->wr_addr_o;
    wdata = t->wr_wdata_o;
    t->eval();
  }
  t->clk_i = b;
  t->accel_clk_i = a;
  t->eval();
  if (b) {
    for (int p = 0; p < static_cast<int>(CHIPLEAP_PORTS); p++) {
      if (bit_of(t->rd_rvalid_i, p)) rq[p].pop_front();
      if (take_r[p]) rq[p].push_back({g_cycle + static_cast<uint64_t>(latency), raddr[p]});
    }
    if (wrv) wq.pop_front();
    if (wg) {
      mem_wr(waddr, wdata);
      wq.push_back(g_cycle + static_cast<uint64_t>(latency));
    }
    g_cycle++;
    t_bus += bus_ps;
  }
  if (a) {
    g_acycle++;
    t_acc += acc_ps;
  }
}

// Edges up to and including the next bus edge.
static void tick() {
  const uint64_t c = g_cycle;
  while (g_cycle == c) edge();
}

// The register interface holds valid until ready, which comes back through the crossing.
static uint32_t reg_access(uint32_t off, bool write, uint32_t val) {
  g_top->reg_valid_i = 1;
  g_top->reg_write_i = write;
  g_top->reg_addr_i = off;
  g_top->reg_wdata_i = val;
  g_top->reg_wstrb_i = 0xF;
  uint32_t v = 0;
  for (uint64_t n = 0;; n++) {
    g_top->clk_i = 0;
    g_top->eval();
    if (g_top->reg_ready_o) {
      v = g_top->reg_rdata_o;
      tick();
      break;
    }
    if (n > 100000) {
      std::printf("{\"error\":\"register access did not complete\"}\n");
      std::exit(1);
    }
    tick();
  }
  g_top->reg_valid_i = 0;
  g_top->reg_write_i = 0;
  return v;
}
static void reg_write(uint32_t off, uint32_t val) { reg_access(off, true, val); }
static uint32_t reg_read(uint32_t off) { return reg_access(off, false, 0); }

static int8_t wval(const std::vector<uint8_t> &w, uint32_t row_bytes, uint32_t r, uint32_t e,
                   uint32_t fmt) {
  uint32_t bitpos = e * fmt;
  uint8_t byte = w[r * row_bytes + bitpos / 8];
  uint32_t v = (byte >> (bitpos % 8)) & ((1u << fmt) - 1u);
  if (v & (1u << (fmt - 1))) v |= ~((1u << fmt) - 1u);
  return static_cast<int8_t>(v);
}

int main(int argc, char **argv) {
  std::vector<std::string> args;
  for (int i = 1; i < argc; i++)
    if (argv[i][0] != '+') args.emplace_back(argv[i]);
  if (args.size() != 3) {
    std::fprintf(stderr, "usage: %s BUS_PS ACCEL_PS ACCEL_PHASE_PS < cases\n", argv[0]);
    return 2;
  }
  bus_ps = std::stoull(args[0]);
  acc_ps = std::stoull(args[1]);
  t_acc = std::stoull(args[2]);
  if (!bus_ps || !acc_ps) {
    std::fprintf(stderr, "clock periods must be positive\n");
    return 2;
  }
  Verilated::commandArgs(argc, argv);
  VerilatedContext ctx;
  Vchipleap_accel_cdc top{&ctx};
  g_top = &top;
  mem.assign(32u << 20, 0);

  top.rst_ni = 0;
  top.reg_valid_i = 0;
  for (int i = 0; i < 4; i++) tick();
  while (g_acycle < 4) edge();
  top.rst_ni = 1;
  for (int i = 0; i < 4; i++) tick();
  while (g_acycle < 12) edge();

  std::string line;
  int failures = 0;
  while (std::getline(std::cin, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::istringstream in(line);
    uint32_t m, k, n, fmt, seed;
    in >> m >> k >> n >> fmt >> seed >> stall_pct >> latency;
    if (latency < 1) latency = 1;
    rng.seed(seed);

    chipleap_plan_t plan;
    int rc = chipleap_plan(m, k, n, fmt, &plan);
    if (rc != 0) {
      std::printf("{\"m\":%u,\"k\":%u,\"n\":%u,\"fmt\":%u,\"error\":%d}\n", m, k, n, fmt, rc);
      failures++;
      continue;
    }
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
    if (Y + m * n * 4 > mem.size()) {
      std::printf("{\"error\":\"case too large for the memory model\"}\n");
      failures++;
      continue;
    }
    std::copy(x.begin(), x.end(), mem.begin() + X);
    std::copy(w.begin(), w.end(), mem.begin() + W);
    std::fill(mem.begin() + Y, mem.begin() + Y + m * n * 4, 0xA5);

    chipleap_start(0, X, W, Y, m, n, fmt, &plan);
    uint64_t t0 = g_cycle;
    uint64_t limit = 200000000ull;
    while (chipleap_busy(0)) {
      if (g_cycle - t0 > limit) break;
    }
    uint64_t wall = g_cycle - t0;
    uint32_t cycles = chipleap_cycles(0);

    uint32_t bad = 0;
    for (uint32_t i = 0; i < m; i++)
      for (uint32_t j = 0; j < n; j++) {
        int32_t ref = 0;
        for (uint32_t e = 0; e < k; e++)
          ref += static_cast<int32_t>(static_cast<int8_t>(x[i * kp + e])) * wval(w, w_row, j, e, fmt);
        int32_t got = static_cast<int32_t>(mem_rd(Y + (i * n + j) * 4));
        if (got != ref) {
          if (bad < 3)
            std::fprintf(stderr, "mismatch y[%u][%u]: got %d expected %d\n", i, j, got, ref);
          bad++;
        }
      }
    if (bad) failures++;
    std::printf(
        "{\"m\":%u,\"k\":%u,\"n\":%u,\"fmt\":%u,\"stall\":%d,\"latency\":%d,\"tm\":%u,"
        "\"tn\":%u,\"tk\":%u,\"x_resident\":%u,\"cycles\":%u,\"wall\":%llu,\"mismatches\":%u,"
        "\"ok\":%s}\n",
        m, k, n, fmt, stall_pct, latency, plan.tm, plan.tn, plan.tk, plan.x_resident, cycles,
        static_cast<unsigned long long>(wall), bad, bad ? "false" : "true");
    std::fflush(stdout);
  }
  top.final();
  return failures ? 1 : 0;
}

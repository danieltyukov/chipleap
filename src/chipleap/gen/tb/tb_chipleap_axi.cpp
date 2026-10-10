// Verilator testbench for the ChipLeap accelerator behind its AXI4 wrapper.
//
// Same cases and checks as tb_chipleap.cpp, but the host side is AMBA: registers are
// programmed over AXI4-Lite and memory is an AXI4 slave model. The model holds ARREADY,
// AWREADY and WREADY low at random (stall_pct, each channel drawn on its own), answers
// reads and writes a fixed latency after the handshake, and checks that every read is a
// single 32-bit INCR beat. Reads per port return in order, as one ID requires.
//
// Input, one case per line: "m k n fmt seed stall_pct latency". Output: one JSON line per
// case with the cycle count from the CYCLES register.
//
// SPDX-License-Identifier: MIT

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "Vchipleap_accel_axi.h"
#include "verilated.h"

#ifndef CHIPLEAP_AXI_ID_WIDTH
#define CHIPLEAP_AXI_ID_WIDTH 1
#endif

static Vchipleap_accel_axi *g_top = nullptr;
static uint64_t g_cycle = 0;
static void reg_write(uint32_t off, uint32_t val);
static uint32_t reg_read(uint32_t off);

#define CHIPLEAP_BASE 0u
#define CHIPLEAP_WRITE(base, off, val) reg_write((off), (val))
#define CHIPLEAP_READ(base, off) reg_read((off))
#include "chipleap_driver.h"

// Bit-field access for Verilator's port types, whose width depends on the parameters.
template <typename T> static uint64_t get_bits(const T &v, int lo, int w) {
  uint64_t x = static_cast<uint64_t>(v) >> lo;
  return w >= 64 ? x : (x & ((1ull << w) - 1));
}
template <std::size_t N> static uint64_t get_bits(const VlWide<N> &v, int lo, int w) {
  uint64_t r = 0;
  for (int i = 0; i < w; i++) {
    int b = lo + i;
    r |= static_cast<uint64_t>((v[b / 32] >> (b % 32)) & 1u) << i;
  }
  return r;
}
template <typename T> static void set_bits(T &v, int lo, int w, uint64_t val) {
  uint64_t x = static_cast<uint64_t>(v);
  uint64_t mask = (w >= 64 ? ~0ull : ((1ull << w) - 1)) << lo;
  x = (x & ~mask) | ((val << lo) & mask);
  v = static_cast<T>(x);
}
template <std::size_t N> static void set_bits(VlWide<N> &v, int lo, int w, uint64_t val) {
  for (int i = 0; i < w; i++) {
    int b = lo + i;
    uint32_t bit = 1u << (b % 32);
    if ((val >> i) & 1u) v[b / 32] |= bit;
    else v[b / 32] &= ~bit;
  }
}

struct Read {
  uint64_t ready;
  uint32_t addr;
  uint64_t id;
};
struct Resp {
  uint64_t ready;
  uint64_t id;
};

static const int P = static_cast<int>(CHIPLEAP_PORTS);
static const int IDW = CHIPLEAP_AXI_ID_WIDTH;
static std::vector<uint8_t> mem;
static std::deque<Read> rq[CHIPLEAP_PORTS];
static std::deque<std::pair<uint32_t, uint64_t>> awq;  // address, id
static std::deque<std::pair<uint32_t, uint32_t>> wq;   // data, strobes
static std::deque<Resp> bq;
static std::mt19937 rng;
static int stall_pct = 0;
static int latency = 1;
static int protocol_errors = 0;

// AXI4-Lite handshakes seen on the last clock edge, for the register helpers.
static bool lite_w_hs = false, lite_b_hs = false, lite_ar_hs = false, lite_r_hs = false;
static uint32_t lite_rdata = 0;

static uint32_t mem_rd(uint32_t a) {
  return mem[a] | (mem[a + 1] << 8) | (mem[a + 2] << 16) | (static_cast<uint32_t>(mem[a + 3]) << 24);
}
static void mem_wr(uint32_t a, uint32_t v, uint32_t strb) {
  for (int i = 0; i < 4; i++)
    if ((strb >> i) & 1u) mem[a + i] = (v >> (8 * i)) & 0xFF;
}

static bool ready() { return stall_pct == 0 || static_cast<int>(rng() % 100) >= stall_pct; }

// One clock cycle: drive the slave side for the current master outputs, sample every
// handshake, then a rising edge.
static void tick() {
  Vchipleap_accel_axi *t = g_top;
  t->clk_i = 0;
  t->eval();
  bool ar_hs[CHIPLEAP_PORTS], r_hs[CHIPLEAP_PORTS];
  for (int p = 0; p < P; p++) {
    set_bits(t->m_axi_arready_i, p, 1, ready() ? 1 : 0);
    bool rv = !rq[p].empty() && rq[p].front().ready <= g_cycle;
    set_bits(t->m_axi_rvalid_i, p, 1, rv ? 1 : 0);
    set_bits(t->m_axi_rlast_i, p, 1, 1);
    set_bits(t->m_axi_rresp_i, 2 * p, 2, 0);
    if (rv) {
      set_bits(t->m_axi_rdata_i, 32 * p, 32, mem_rd(rq[p].front().addr));
      set_bits(t->m_axi_rid_i, IDW * p, IDW, rq[p].front().id);
    }
  }
  t->m_axi_awready_i = ready();
  t->m_axi_wready_i = ready();
  bool bv = !bq.empty() && bq.front().ready <= g_cycle;
  t->m_axi_bvalid_i = bv;
  t->m_axi_bresp_i = 0;
  if (bv) set_bits(t->m_axi_bid_i, 0, IDW, bq.front().id);
  t->s_axil_bready_i = 1;
  t->s_axil_rready_i = 1;
  t->eval();

  uint32_t raddr[CHIPLEAP_PORTS];
  uint64_t rid[CHIPLEAP_PORTS];
  for (int p = 0; p < P; p++) {
    ar_hs[p] = get_bits(t->m_axi_arvalid_o, p, 1) && get_bits(t->m_axi_arready_i, p, 1);
    r_hs[p] = get_bits(t->m_axi_rvalid_i, p, 1) && get_bits(t->m_axi_rready_o, p, 1);
    raddr[p] = static_cast<uint32_t>(get_bits(t->m_axi_araddr_o, 32 * p, 32));
    rid[p] = get_bits(t->m_axi_arid_o, IDW * p, IDW);
    if (ar_hs[p] && (get_bits(t->m_axi_arlen_o, 8 * p, 8) != 0 ||
                     get_bits(t->m_axi_arsize_o, 3 * p, 3) != 2 ||
                     get_bits(t->m_axi_arburst_o, 2 * p, 2) != 1 || (raddr[p] & 3u))) {
      protocol_errors++;
    }
  }
  bool aw_hs = t->m_axi_awvalid_o && t->m_axi_awready_i;
  bool w_hs = t->m_axi_wvalid_o && t->m_axi_wready_i;
  bool b_hs = bv && t->m_axi_bready_o;
  if (aw_hs && (t->m_axi_awlen_o != 0 || t->m_axi_awsize_o != 2 || (t->m_axi_awaddr_o & 3u)))
    protocol_errors++;
  if (w_hs && !t->m_axi_wlast_o) protocol_errors++;
  uint32_t awaddr = t->m_axi_awaddr_o, wdata = t->m_axi_wdata_o, wstrb = t->m_axi_wstrb_o;
  uint64_t awid = static_cast<uint64_t>(t->m_axi_awid_o);
  lite_w_hs = t->s_axil_awvalid_i && t->s_axil_awready_o && t->s_axil_wvalid_i &&
              t->s_axil_wready_o;
  lite_ar_hs = t->s_axil_arvalid_i && t->s_axil_arready_o;
  lite_b_hs = t->s_axil_bvalid_o && t->s_axil_bready_i;
  lite_r_hs = t->s_axil_rvalid_o && t->s_axil_rready_i;
  if (lite_r_hs) lite_rdata = t->s_axil_rdata_o;

  t->clk_i = 1;
  t->eval();

  for (int p = 0; p < P; p++) {
    if (r_hs[p]) rq[p].pop_front();
    if (ar_hs[p]) rq[p].push_back({g_cycle + static_cast<uint64_t>(latency), raddr[p], rid[p]});
  }
  if (b_hs) bq.pop_front();
  if (aw_hs) awq.push_back({awaddr, awid});
  if (w_hs) wq.push_back({wdata, wstrb});
  while (!awq.empty() && !wq.empty()) {
    mem_wr(awq.front().first, wq.front().first, wq.front().second);
    bq.push_back({g_cycle + static_cast<uint64_t>(latency), awq.front().second});
    awq.pop_front();
    wq.pop_front();
  }
  g_cycle++;
}

static void reg_write(uint32_t off, uint32_t val) {
  g_top->s_axil_awaddr_i = off;
  g_top->s_axil_awvalid_i = 1;
  g_top->s_axil_wdata_i = val;
  g_top->s_axil_wstrb_i = 0xF;
  g_top->s_axil_wvalid_i = 1;
  do tick();
  while (!lite_w_hs);
  g_top->s_axil_awvalid_i = 0;
  g_top->s_axil_wvalid_i = 0;
  do tick();
  while (!lite_b_hs);
}

static uint32_t reg_read(uint32_t off) {
  g_top->s_axil_araddr_i = off;
  g_top->s_axil_arvalid_i = 1;
  do tick();
  while (!lite_ar_hs);
  g_top->s_axil_arvalid_i = 0;
  do tick();
  while (!lite_r_hs);
  return lite_rdata;
}

static int8_t wval(const std::vector<uint8_t> &w, uint32_t row_bytes, uint32_t r, uint32_t e,
                   uint32_t fmt) {
  uint32_t bitpos = e * fmt;
  uint8_t byte = w[r * row_bytes + bitpos / 8];
  uint32_t v = (byte >> (bitpos % 8)) & ((1u << fmt) - 1u);
  if (v & (1u << (fmt - 1))) v |= ~((1u << fmt) - 1u);
  return static_cast<int8_t>(v);
}

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VerilatedContext ctx;
  Vchipleap_accel_axi top{&ctx};
  g_top = &top;
  mem.assign(32u << 20, 0);

  top.rst_ni = 0;
  top.s_axil_awvalid_i = 0;
  top.s_axil_wvalid_i = 0;
  top.s_axil_arvalid_i = 0;
  for (int i = 0; i < 4; i++) tick();
  top.rst_ni = 1;
  tick();

  std::string line;
  int failures = 0;
  while (std::getline(std::cin, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::istringstream in(line);
    uint32_t m, k, n, fmt, seed;
    in >> m >> k >> n >> fmt >> seed >> stall_pct >> latency;
    if (latency < 1) latency = 1;
    rng.seed(seed);
    protocol_errors = 0;

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

    chipleap_start(CHIPLEAP_BASE, X, W, Y, m, n, fmt, &plan);
    uint64_t t0 = g_cycle;
    uint64_t limit = 200000000ull;
    while (chipleap_busy(CHIPLEAP_BASE)) {
      if (g_cycle - t0 > limit) break;
    }
    uint64_t wall = g_cycle - t0;
    uint32_t cycles = chipleap_cycles(CHIPLEAP_BASE);

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
    bool ok = bad == 0 && protocol_errors == 0;
    if (!ok) failures++;
    std::printf(
        "{\"m\":%u,\"k\":%u,\"n\":%u,\"fmt\":%u,\"stall\":%d,\"latency\":%d,\"tm\":%u,"
        "\"tn\":%u,\"tk\":%u,\"x_resident\":%u,\"cycles\":%u,\"wall\":%llu,\"mismatches\":%u,"
        "\"protocol_errors\":%d,\"ok\":%s}\n",
        m, k, n, fmt, stall_pct, latency, plan.tm, plan.tn, plan.tk, plan.x_resident, cycles,
        static_cast<unsigned long long>(wall), bad, protocol_errors, ok ? "true" : "false");
    std::fflush(stdout);
  }
  top.final();
  return failures ? 1 : 0;
}

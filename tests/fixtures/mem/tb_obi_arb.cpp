// Verilator test of chipleap_obi_arb (chipleap_mem.sv): N random OBI masters against one
// slave that grants at random, keeps requests in flight and answers in order after a
// random latency.
//
// Arguments: cycles seed req_pct gnt_pct max_latency. Each master holds up to four
// requests in flight; an idle master starts one with probability req_pct percent per
// cycle (100: it always requests). Prints one JSON line:
//   grants per master, the most grants to other masters while a master's request waited,
//   the most requests in flight at the slave, responses with the wrong master, order or
//   data, and slave requests that changed or dropped before their grant.
//
// SPDX-License-Identifier: MIT

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <random>
#include <vector>

#include "Vchipleap_obi_arb.h"
#include "verilated.h"

#ifndef ARB_N
#define ARB_N 2
#endif
static const int N = ARB_N;

template <typename T> static uint32_t word_of(const T &v, int i) {
  return static_cast<uint32_t>(static_cast<uint64_t>(v) >> (32 * i));
}
template <std::size_t W> static uint32_t word_of(const VlWide<W> &v, int i) { return v[i]; }
template <typename T> static void set_word(T &v, int i, uint32_t w) {
  uint64_t x = static_cast<uint64_t>(v);
  x &= ~(0xFFFFFFFFull << (32 * i));
  x |= static_cast<uint64_t>(w) << (32 * i);
  v = static_cast<T>(x);
}
template <std::size_t W> static void set_word(VlWide<W> &v, int i, uint32_t w) { v[i] = w; }
template <typename T> static bool bit_of(const T &v, int i) { return (v >> i) & 1u; }
template <typename T> static void set_bit(T &v, int i, bool b) {
  if (b) v |= static_cast<T>(1ull << i);
  else v &= static_cast<T>(~(1ull << i));
}

struct Req {
  bool we;
  uint32_t addr, wdata;
};

int main(int argc, char **argv) {
  if (argc < 6) {
    std::fprintf(stderr, "usage: %s CYCLES SEED REQ_PCT GNT_PCT MAX_LATENCY\n", argv[0]);
    return 2;
  }
  const long cycles = std::atol(argv[1]);
  std::mt19937 rng(static_cast<uint32_t>(std::atol(argv[2])));
  const int req_pct = std::atoi(argv[3]), gnt_pct = std::atoi(argv[4]);
  const int max_lat = std::max(1, std::atoi(argv[5]));
  Verilated::commandArgs(argc, argv);
  VerilatedContext ctx;
  Vchipleap_obi_arb t{&ctx};

  std::vector<uint32_t> mem(1024);
  for (auto &w : mem) w = static_cast<uint32_t>(rng());
  // Each master: its request on the port (if any), and its requests in flight with the
  // data each read must return.
  std::vector<bool> req(N, false);
  std::vector<Req> cur(N);
  std::vector<std::deque<std::pair<bool, uint32_t>>> inflight(N);
  std::vector<long> others(N, 0);  // grants to other masters since this one's request
  // The slave's requests in flight: (master, ready cycle, rdata).
  struct S {
    int m;
    long ready;
    uint32_t rdata;
  };
  std::deque<S> sq;
  std::vector<long> grants(N, 0);
  long worst_wait = 0, bad = 0, obi_bad = 0;
  size_t most_inflight = 0;
  bool held = false;
  Req held_req{};

  t.rst_ni = 0;
  t.clk_i = 0;
  t.eval();
  t.clk_i = 1;
  t.eval();
  t.rst_ni = 1;
  for (long c = 0; c < cycles; c++) {
    t.clk_i = 0;
    for (int i = 0; i < N; i++) {
      if (!req[i] && inflight[i].size() < 4 && static_cast<int>(rng() % 100) < req_pct) {
        req[i] = true;
        bool we = rng() & 1u;
        uint32_t addr = static_cast<uint32_t>(rng() % 1024) * 4;
        cur[i] = {we, addr, static_cast<uint32_t>(rng())};
        others[i] = 0;
      }
      set_bit(t.m_req_i, i, req[i]);
      set_bit(t.m_we_i, i, cur[i].we);
      set_word(t.m_addr_i, i, cur[i].addr);
      set_word(t.m_wdata_i, i, cur[i].wdata);
    }
    t.m_be_i = static_cast<uint16_t>((1u << (4 * N)) - 1u);
    bool rv = !sq.empty() && sq.front().ready <= c;
    t.s_rvalid_i = rv;
    t.s_rdata_i = rv ? sq.front().rdata : 0;
    t.eval();
    bool sreq = t.s_req_o;
    Req s{static_cast<bool>(t.s_we_o), t.s_addr_o, t.s_wdata_o};
    if (held && (!sreq || s.we != held_req.we || s.addr != held_req.addr ||
                 (s.we && s.wdata != held_req.wdata)))
      obi_bad++;
    bool g = sreq && static_cast<int>(rng() % 100) < gnt_pct;
    t.s_gnt_i = g;
    held = sreq && !g;
    held_req = s;
    t.eval();
    // Responses to the masters.
    int got = -1;
    for (int i = 0; i < N; i++)
      if (bit_of(t.m_rvalid_o, i)) {
        if (got >= 0) bad++;
        got = i;
      }
    if (rv) {
      const S &f = sq.front();
      if (got != f.m || inflight[f.m].empty()) {
        bad++;
      } else {
        auto [we, want] = inflight[f.m].front();
        inflight[f.m].pop_front();
        if (!we && t.m_rdata_o != want) bad++;
      }
    } else if (got >= 0) {
      bad++;
    }
    // Grants: exactly the master the slave saw, only when the slave granted.
    int gm = -1;
    for (int i = 0; i < N; i++)
      if (bit_of(t.m_gnt_o, i)) {
        if (gm >= 0 || !req[i]) bad++;
        gm = i;
      }
    if (g != (gm >= 0)) bad++;
    if (gm >= 0) {
      const Req &r = cur[gm];
      if (r.we != s.we || r.addr != s.addr || (r.we && r.wdata != s.wdata)) bad++;
      uint32_t rdata = 0;
      if (r.we) mem[r.addr / 4] = r.wdata;
      else rdata = mem[r.addr / 4];
      inflight[gm].push_back({r.we, rdata});
      sq.push_back({gm, c + 1 + static_cast<long>(rng() % max_lat), rdata});
      most_inflight = std::max(most_inflight, sq.size());
      grants[gm]++;
      req[gm] = false;
      for (int i = 0; i < N; i++)
        if (i != gm && req[i]) worst_wait = std::max(worst_wait, ++others[i]);
    }
    if (rv) sq.pop_front();
    t.clk_i = 1;
    t.eval();
  }
  std::printf("{\"grants\":[");
  for (int i = 0; i < N; i++) std::printf("%s%ld", i ? "," : "", grants[i]);
  std::printf("],\"worst_wait\":%ld,\"most_inflight\":%zu,\"bad\":%ld,\"obi_bad\":%ld}\n",
              worst_wait, most_inflight, bad, obi_bad);
  t.final();
  return 0;
}

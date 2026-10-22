// Verilator testbench for the accelerator's direct read path to external memory:
// chipleap_accel_mem, the accelerator with EXT_DIRECT and chipleap_mem, the arbiter that
// puts X-HEEP's external slave ports and the accelerator's direct ports on the memory
// ports.
//
// Cases on stdin, one per line: "m k n fmt seed stall_pct latency ext_latency x_pct place".
// The on-chip memory model serves rd_* and wr_* and answers `latency` bus cycles after a
// grant; the external memory model serves the memory ports, accepts requests while
// earlier ones are in flight and answers in order `ext_latency` cycles of the memory
// ports' clock after each grant. Both grant each request with probability
// 100 - stall_pct percent in a cycle. place: bit 0 puts the weights in external memory,
// bit 1 the activations; the results stay on chip. X-HEEP's NX external slave ports are
// traffic generators: an idle port starts a request with probability x_pct percent per bus
// cycle (a read, or a write with random byte enables, to a region of external memory of
// its own), holds it until granted and waits for its response, and every read is checked
// against a shadow copy of the region.
//
// The clocks are "bus_ps accel_ps accel_phase_ps" on the command line. With
// CHIPLEAP_MEM_CDC the accelerator runs on its own clock behind its crossings and the
// memory ports run on that clock; otherwise everything runs on the bus clock.
//
// Each result line has the job's cycle count (the accelerator's clock), its check, and of
// the memory path: the most requests in flight at memory port 0, X-HEEP's requests and
// how many of its reads were wrong (or of its writes lost: its regions are compared with
// the shadow after each case), the longest an X-HEEP request waited for its grant and for
// its response (bus cycles), requests at the memory ports that changed or dropped before their grant
// (OBI violations), and the longest run of grants to the accelerator at memory port 0
// while an X-HEEP request was waiting for one.
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

#include "Vchipleap_accel_mem.h"
#include "verilated.h"

static Vchipleap_accel_mem *g_top = nullptr;
static uint64_t g_cycle = 0;   // bus cycles
static uint64_t g_mcycle = 0;  // memory-port cycles
static uint64_t bus_ps = 10000, acc_ps = 10000, t_bus = 0, t_acc = 0;
static void reg_write(uint32_t off, uint32_t val);
static uint32_t reg_read(uint32_t off);

#define CHIPLEAP_WRITE(base, off, val) reg_write((off), (val))
#define CHIPLEAP_READ(base, off) reg_read((off))
#include "chipleap_driver.h"

#ifndef CHIPLEAP_MEM_NX
#define CHIPLEAP_MEM_NX 3
#endif
#ifndef CHIPLEAP_EXT_BASE
#define CHIPLEAP_EXT_BASE 0xC0000000u
#endif
static const int NX = CHIPLEAP_MEM_NX;
static const int P = static_cast<int>(CHIPLEAP_PORTS);

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
template <typename T> static uint32_t nib_of(const T &v, int i) {
  return static_cast<uint32_t>((static_cast<uint64_t>(v) >> (4 * i)) & 0xFu);
}
template <std::size_t N> static uint32_t nib_of(const VlWide<N> &v, int i) {
  return (v[i / 8] >> (4 * (i % 8))) & 0xFu;
}
template <typename T> static void set_nib(T &v, int i, uint32_t n) {
  uint64_t x = static_cast<uint64_t>(v);
  x &= ~(0xFull << (4 * i));
  x |= static_cast<uint64_t>(n & 0xFu) << (4 * i);
  v = static_cast<T>(x);
}
template <typename T> static bool bit_of(const T &v, int i) { return (v >> i) & 1u; }
template <typename T> static void set_bit(T &v, int i, bool b) {
  if (b) v |= static_cast<T>(1ull << i);
  else v &= static_cast<T>(~(1ull << i));
}

// On-chip memory (rd_*, wr_*) and external memory (the memory ports), byte arrays from 0
// and from CHIPLEAP_EXT_BASE.
static std::vector<uint8_t> mem, ext;
static std::mt19937 rng, xrng;
static int stall_pct = 0, latency = 1, ext_latency = 1, x_pct = 0;

static uint32_t rd32(const std::vector<uint8_t> &m, uint32_t a) {
  return m[a] | (m[a + 1] << 8) | (m[a + 2] << 16) | (static_cast<uint32_t>(m[a + 3]) << 24);
}
static void wr32(std::vector<uint8_t> &m, uint32_t a, uint32_t v, uint32_t be = 0xF) {
  for (int i = 0; i < 4; i++)
    if ((be >> i) & 1u) m[a + i] = (v >> (8 * i)) & 0xFF;
}
static bool is_ext(uint32_t a) { return a >= CHIPLEAP_EXT_BASE; }
static uint32_t ext_off(uint32_t a) { return a - CHIPLEAP_EXT_BASE; }

static bool grant() { return stall_pct == 0 || static_cast<int>(rng() % 100) >= stall_pct; }

struct Pending {
  uint64_t ready;
  uint32_t addr;
};
struct MemPending {
  uint64_t ready;
  uint32_t rdata;
};
static std::deque<Pending> rq[CHIPLEAP_PORTS];
static std::deque<uint64_t> wq;
static std::deque<MemPending> mq[CHIPLEAP_PORTS];

// X-HEEP's ports: idle, requesting (until granted) or waiting for the response.
const uint32_t X_REGION = 0x01000000u;  // offset in external memory of port 0's region
const uint32_t X_REGION_BYTES = 0x1000u;
struct XPort {
  int state = 0;  // 0 idle, 1 requesting, 2 waiting
  bool we = false;
  uint32_t be = 0xF, addr = 0, wdata = 0, expect = 0;
  uint64_t since = 0;
};
static XPort xp[CHIPLEAP_MEM_NX];
static std::vector<uint8_t> shadow;  // X-HEEP's regions as the memory must hold them

// The memory path's statistics for the current case.
struct Stats {
  uint32_t max_inflight = 0, x_requests = 0, x_reads = 0, x_bad = 0, obi_bad = 0;
  uint64_t x_max_wait = 0, x_max_latency = 0;
  uint32_t run = 0, max_run = 0;
};
static Stats st;
// The memory ports' requests that were not granted, to check they hold.
struct Held {
  bool v = false, we = false;
  uint32_t be = 0, addr = 0, wdata = 0;
};
static Held held[CHIPLEAP_PORTS];
static uint32_t inflight0 = 0;

static void x_start(int i) {
  XPort &x = xp[i];
  x.state = 1;
  x.since = g_cycle;
  x.we = xrng() & 1u;
  x.be = x.we ? (xrng() & 0xFu) : 0xFu;
  if (x.we && !x.be) x.be = 0x1;
  x.addr = CHIPLEAP_EXT_BASE + X_REGION + i * X_REGION_BYTES + (xrng() % (X_REGION_BYTES / 4)) * 4;
  x.wdata = xrng();
  st.x_requests++;
}

static bool x_waiting() {
  for (int i = 0; i < NX; i++)
    if (xp[i].state == 1) return true;
  return false;
}

// The next rising edge of either clock, or of both when they coincide. On an edge each
// model presents its grants and responses for the request state before it.
static void edge() {
  Vchipleap_accel_mem *t = g_top;
  const uint64_t now = std::min(t_bus, t_acc);
  const bool b = t_bus == now, a = t_acc == now;
#ifdef CHIPLEAP_MEM_CDC
  const bool m = a;
#else
  const bool m = b;
#endif
  t->clk_i = 0;
  t->accel_clk_i = 0;
  // X-HEEP's ports present their requests; responses from both memories.
  if (b) {
    for (int i = 0; i < NX; i++) {
      if (xp[i].state == 0 && x_pct && static_cast<int>(xrng() % 100) < x_pct) x_start(i);
      set_bit(t->x_req_i, i, xp[i].state == 1);
      set_bit(t->x_we_i, i, xp[i].we);
      set_nib(t->x_be_i, i, xp[i].be);
      set_word(t->x_addr_i, i, xp[i].addr);
      set_word(t->x_wdata_i, i, xp[i].wdata);
    }
    for (int p = 0; p < P; p++) {
      bool rv = !rq[p].empty() && rq[p].front().ready <= g_cycle;
      set_bit(t->rd_rvalid_i, p, rv);
      if (rv) set_word(t->rd_rdata_i, p, rd32(mem, rq[p].front().addr));
    }
    t->wr_rvalid_i = !wq.empty() && wq.front() <= g_cycle;
  }
  if (m) {
    for (int p = 0; p < P; p++) {
      bool rv = !mq[p].empty() && mq[p].front().ready <= g_mcycle;
      set_bit(t->mem_rvalid_i, p, rv);
      if (rv) set_word(t->mem_rdata_i, p, mq[p].front().rdata);
    }
  }
  t->eval();
  // Grants, for the requests the responses above leave.
  bool take_r[CHIPLEAP_PORTS] = {}, take_m[CHIPLEAP_PORTS] = {};
  bool wg = false, wrv = false;
  uint32_t waddr = 0, wdata = 0, raddr[CHIPLEAP_PORTS] = {};
  if (b) {
    for (int p = 0; p < P; p++) {
      bool g = bit_of(t->rd_req_o, p) && grant();
      set_bit(t->rd_gnt_i, p, g);
      take_r[p] = g;
      raddr[p] = word_of(t->rd_addr_o, p);
    }
    wg = t->wr_req_o && grant();
    t->wr_gnt_i = wg;
    wrv = t->wr_rvalid_i;
    waddr = t->wr_addr_o;
    wdata = t->wr_wdata_o;
  }
  uint32_t m_addr[CHIPLEAP_PORTS] = {}, m_wdata[CHIPLEAP_PORTS] = {}, m_be[CHIPLEAP_PORTS] = {};
  bool m_we[CHIPLEAP_PORTS] = {};
  if (m) {
    for (int p = 0; p < P; p++) {
      bool req = bit_of(t->mem_req_o, p);
      m_addr[p] = word_of(t->mem_addr_o, p);
      m_wdata[p] = word_of(t->mem_wdata_o, p);
      m_be[p] = nib_of(t->mem_be_o, p);
      m_we[p] = bit_of(t->mem_we_o, p);
      // A request not granted before must still be there, unchanged.
      if (held[p].v && (!req || held[p].addr != m_addr[p] || held[p].we != m_we[p] ||
                        (m_we[p] && (held[p].wdata != m_wdata[p] || held[p].be != m_be[p]))))
        st.obi_bad++;
      bool g = req && grant();
      set_bit(t->mem_gnt_i, p, g);
      take_m[p] = g;
      held[p] = {req && !g, m_we[p], m_be[p], m_addr[p], m_wdata[p]};
    }
  }
  t->eval();
  // X-HEEP's ports see their grants and responses.
  bool x_gnt[CHIPLEAP_MEM_NX] = {}, x_rv[CHIPLEAP_MEM_NX] = {};
  uint32_t x_rd[CHIPLEAP_MEM_NX] = {};
  if (b) {
    for (int i = 0; i < NX; i++) {
      x_gnt[i] = xp[i].state == 1 && bit_of(t->x_gnt_o, i);
      x_rv[i] = bit_of(t->x_rvalid_o, i);
      x_rd[i] = word_of(t->x_rdata_o, i);
    }
  }
  t->clk_i = b;
  t->accel_clk_i = a;
  t->eval();
  if (m) {
    for (int p = 0; p < P; p++) {
      if (bit_of(t->mem_rvalid_i, p)) {
        mq[p].pop_front();
        if (p == 0) inflight0--;
      }
      if (!take_m[p]) continue;
      if (!is_ext(m_addr[p])) {
        std::fprintf(stderr, "memory port %d: address %08x outside external memory\n", p,
                     m_addr[p]);
        st.obi_bad++;
        continue;
      }
      uint32_t off = ext_off(m_addr[p]);
      uint32_t rdata = 0;
      if (m_we[p]) wr32(ext, off, m_wdata[p], m_be[p]);
      else rdata = rd32(ext, off);
      mq[p].push_back({g_mcycle + static_cast<uint64_t>(ext_latency), rdata});
      if (p == 0) {
        inflight0++;
        st.max_inflight = std::max(st.max_inflight, inflight0);
        // X-HEEP's regions lie above X_REGION; everything below is the accelerator's.
        bool accel = off < X_REGION;
        if (accel && x_waiting()) st.max_run = std::max(st.max_run, ++st.run);
        else st.run = 0;
      }
    }
    g_mcycle++;
  }
  if (b) {
    for (int p = 0; p < P; p++) {
      if (bit_of(t->rd_rvalid_i, p)) rq[p].pop_front();
      if (take_r[p]) rq[p].push_back({g_cycle + static_cast<uint64_t>(latency), raddr[p]});
    }
    if (wrv) wq.pop_front();
    if (wg) {
      wr32(mem, waddr, wdata);
      wq.push_back(g_cycle + static_cast<uint64_t>(latency));
    }
    for (int i = 0; i < NX; i++) {
      XPort &x = xp[i];
      if (x.state == 2 && x_rv[i]) {
        st.x_max_latency = std::max(st.x_max_latency, g_cycle - x.since);
        if (!x.we) {
          st.x_reads++;
          if (x_rd[i] != x.expect) {
            if (st.x_bad < 3)
              std::fprintf(stderr, "X-HEEP port %d read %08x: got %08x expected %08x\n", i,
                           x.addr, x_rd[i], x.expect);
            st.x_bad++;
          }
        }
        x.state = 0;
      } else if (x_rv[i]) {
        std::fprintf(stderr, "X-HEEP port %d: a response without a request\n", i);
        st.x_bad++;
      }
      if (x_gnt[i]) {
        st.x_max_wait = std::max(st.x_max_wait, g_cycle - x.since);
        uint32_t off = ext_off(x.addr);
        if (x.we) wr32(shadow, off, x.wdata, x.be);
        else x.expect = rd32(shadow, off);
        x.state = 2;
      }
    }
    g_cycle++;
    t_bus += bus_ps;
  }
  if (a) t_acc += acc_ps;
}

// Edges up to and including the next bus edge.
static void tick() {
  const uint64_t c = g_cycle;
  while (g_cycle == c) edge();
}

// The register interface holds valid until ready (at once on the bus clock, later through
// the crossings).
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
  Vchipleap_accel_mem top{&ctx};
  g_top = &top;
  mem.assign(16u << 20, 0);
  ext.assign(X_REGION + NX * X_REGION_BYTES, 0);
  shadow = ext;

  top.rst_ni = 0;
  top.reg_valid_i = 0;
  for (int i = 0; i < 4; i++) tick();
  top.rst_ni = 1;
  for (int i = 0; i < 12; i++) tick();

  std::string line;
  int failures = 0;
  while (std::getline(std::cin, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::istringstream in(line);
    uint32_t m, k, n, fmt, seed, place;
    in >> m >> k >> n >> fmt >> seed >> stall_pct >> latency >> ext_latency >> x_pct >> place;
    latency = std::max(latency, 1);
    ext_latency = std::max(ext_latency, 1);
    rng.seed(seed);
    xrng.seed(seed ^ 0x5EEDu);
    st = Stats{};

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
    // On chip from 0x1000 and in external memory from its base + 0x1000, in that order:
    // activations, weights, results (always on chip).
    uint32_t on = 0x1000, off = 0x1000;
    auto put = [&](const std::vector<uint8_t> &d, bool external) -> uint32_t {
      uint32_t &at = external ? off : on;
      uint32_t a = at;
      at = (at + static_cast<uint32_t>(d.size()) + 0xFFF) & ~0xFFFu;
      std::vector<uint8_t> &dst = external ? ext : mem;
      if (at > (external ? X_REGION : dst.size())) return 0;
      std::copy(d.begin(), d.end(), dst.begin() + a);
      return external ? CHIPLEAP_EXT_BASE + a : a;
    };
    const uint32_t X = put(x, place & 2u), W = put(w, place & 1u);
    const uint32_t Y = on;
    if (!X || !W || Y + m * n * 4 > mem.size()) {
      std::printf("{\"error\":\"case too large for the memory model\"}\n");
      failures++;
      continue;
    }
    std::fill(mem.begin() + Y, mem.begin() + Y + m * n * 4, 0xA5);

    chipleap_start(0, X, W, Y, m, n, fmt, &plan);
    uint64_t t0 = g_cycle;
    uint64_t limit = 200000000ull;
    while (chipleap_busy(0)) {
      if (g_cycle - t0 > limit) break;
    }
    uint64_t wall = g_cycle - t0;
    uint32_t cycles = chipleap_cycles(0);
    // X-HEEP's last requests finish, then its ports stay idle until the next case.
    const int pct = x_pct;
    x_pct = 0;
    for (int i = 0; i < 1000; i++) {
      bool busy = false;
      for (int j = 0; j < NX; j++) busy = busy || xp[j].state != 0;
      if (!busy) break;
      tick();
    }
    x_pct = pct;
    if (!std::equal(ext.begin() + X_REGION, ext.end(), shadow.begin() + X_REGION)) {
      std::fprintf(stderr, "X-HEEP's regions of external memory differ from the shadow\n");
      st.x_bad++;
    }

    uint32_t bad = 0;
    for (uint32_t i = 0; i < m; i++)
      for (uint32_t j = 0; j < n; j++) {
        int32_t ref = 0;
        for (uint32_t e = 0; e < k; e++)
          ref += static_cast<int32_t>(static_cast<int8_t>(x[i * kp + e])) * wval(w, w_row, j, e, fmt);
        int32_t got = static_cast<int32_t>(rd32(mem, Y + (i * n + j) * 4));
        if (got != ref) {
          if (bad < 3)
            std::fprintf(stderr, "mismatch y[%u][%u]: got %d expected %d\n", i, j, got, ref);
          bad++;
        }
      }
    bool ok = !bad && !st.x_bad && !st.obi_bad;
    if (!ok) failures++;
    std::printf(
        "{\"m\":%u,\"k\":%u,\"n\":%u,\"fmt\":%u,\"stall\":%d,\"latency\":%d,\"ext_latency\":%d,"
        "\"x_pct\":%d,\"place\":%u,\"tm\":%u,\"tn\":%u,\"tk\":%u,\"x_resident\":%u,"
        "\"cycles\":%u,\"wall\":%llu,\"mismatches\":%u,\"max_inflight\":%u,"
        "\"x_requests\":%u,\"x_reads\":%u,\"x_bad\":%u,\"x_max_wait\":%llu,"
        "\"x_max_latency\":%llu,\"obi_bad\":%u,\"accel_run\":%u,\"ok\":%s}\n",
        m, k, n, fmt, stall_pct, latency, ext_latency, x_pct, place, plan.tm, plan.tn, plan.tk,
        plan.x_resident, cycles, static_cast<unsigned long long>(wall), bad, st.max_inflight,
        st.x_requests, st.x_reads, st.x_bad, static_cast<unsigned long long>(st.x_max_wait),
        static_cast<unsigned long long>(st.x_max_latency), st.obi_bad, st.max_run,
        ok ? "true" : "false");
    std::fflush(stdout);
  }
  top.final();
  return failures ? 1 : 0;
}

/*
 * ChipLeap accelerator driver for X-HEEP firmware. Generated for design "{name}".
 *
 * chipleap_plan() picks tile sizes exactly like chipleap.model.accel.plan_tiles, so the
 * cycle counts the explorer predicts are the ones this driver programs. Override
 * CHIPLEAP_WRITE and CHIPLEAP_READ before including this header to run it somewhere
 * other than memory-mapped I/O (the Verilator testbench does).
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef CHIPLEAP_DRIVER_H
#define CHIPLEAP_DRIVER_H

#include <stdint.h>

#define CHIPLEAP_DATAFLOW {dataflow_id} /* 0 gemv, 1 weight stationary, 2 output stationary */
#define CHIPLEAP_ROWS {rows}u
#define CHIPLEAP_COLS {cols}u
#define CHIPLEAP_WBITS {wbits}u
#define CHIPLEAP_PORTS {ports}u
#define CHIPLEAP_ABUF_BYTES {abuf_bytes}u
#define CHIPLEAP_WBUF_BYTES {wbuf_bytes}u
#define CHIPLEAP_DOUBLE_BUF {double_buf}u
#define CHIPLEAP_ACC_ROWS {acc_rows}u
#define CHIPLEAP_BASE {base:#010x}u

#define CHIPLEAP_REG_CTRL 0x00u
#define CHIPLEAP_REG_STATUS 0x04u
#define CHIPLEAP_REG_X_ADDR 0x08u
#define CHIPLEAP_REG_W_ADDR 0x0Cu
#define CHIPLEAP_REG_Y_ADDR 0x10u
#define CHIPLEAP_REG_M 0x14u
#define CHIPLEAP_REG_K 0x18u
#define CHIPLEAP_REG_N 0x1Cu
#define CHIPLEAP_REG_TM 0x20u
#define CHIPLEAP_REG_TK 0x24u
#define CHIPLEAP_REG_FMT 0x28u
#define CHIPLEAP_REG_XRES 0x2Cu
#define CHIPLEAP_REG_CYCLES 0x30u
#define CHIPLEAP_REG_INFO 0x34u
#define CHIPLEAP_REG_IRQ_EN 0x38u

#ifndef CHIPLEAP_WRITE
#define CHIPLEAP_WRITE(base, off, val) \
  (*(volatile uint32_t *)((uintptr_t)(base) + (off)) = (uint32_t)(val))
#endif
#ifndef CHIPLEAP_READ
#define CHIPLEAP_READ(base, off) (*(volatile uint32_t *)((uintptr_t)(base) + (off)))
#endif

typedef struct {{
  uint32_t k_pad; /* K rounded up to the alignment; pad X and W rows with zeros */
  uint32_t tm;
  uint32_t tn;
  uint32_t tk;
  uint32_t x_resident;
}} chipleap_plan_t;

static inline uint32_t chipleap_gcd(uint32_t a, uint32_t b) {{
  while (b) {{
    uint32_t t = a % b;
    a = b;
    b = t;
  }}
  return a;
}}

static inline uint32_t chipleap_lcm(uint32_t a, uint32_t b) {{ return a / chipleap_gcd(a, b) * b; }}

/* K granularity: whole 32-bit words of X and W, and whatever the core consumes per step. */
static inline uint32_t chipleap_k_align(uint32_t fmt) {{
  uint32_t base = chipleap_lcm(4u, 32u / fmt);
#if CHIPLEAP_DATAFLOW == 0
  return chipleap_lcm(base, CHIPLEAP_COLS);
#elif CHIPLEAP_DATAFLOW == 1
  return chipleap_lcm(base, CHIPLEAP_ROWS);
#else
  return base;
#endif
}}

static inline uint32_t chipleap_min(uint32_t a, uint32_t b) {{ return a < b ? a : b; }}

/* Returns 0 on success, -1 if the format is too wide, -2 if the buffers are too small. */
static inline int chipleap_plan(uint32_t m, uint32_t k, uint32_t n, uint32_t fmt,
                                chipleap_plan_t *p) {{
  uint32_t align, k_pad, slots, w_slot_elems, x_slot, tk;
  (void)n;
  if (fmt > CHIPLEAP_WBITS) return -1;
  align = chipleap_k_align(fmt);
  k_pad = (k + align - 1u) / align * align;
  slots = CHIPLEAP_DOUBLE_BUF ? 2u : 1u;
  w_slot_elems = CHIPLEAP_WBUF_BYTES / slots * 8u / fmt;
#if CHIPLEAP_DATAFLOW == 2
  p->tm = chipleap_min(m, CHIPLEAP_ROWS);
  p->tn = CHIPLEAP_COLS;
#elif CHIPLEAP_DATAFLOW == 0
  p->tm = chipleap_min(m, CHIPLEAP_ACC_ROWS);
  p->tn = CHIPLEAP_ROWS;
#else
  p->tm = chipleap_min(m, CHIPLEAP_ACC_ROWS);
  p->tn = CHIPLEAP_COLS;
#endif
  p->k_pad = k_pad;
  if (p->tm * k_pad <= CHIPLEAP_ABUF_BYTES) {{
    tk = chipleap_min(k_pad, w_slot_elems / p->tn) / align * align;
    if (tk >= align) {{
      p->tk = tk;
      p->x_resident = 1u;
      return 0;
    }}
  }}
  x_slot = CHIPLEAP_ABUF_BYTES / slots;
  tk = chipleap_min(chipleap_min(k_pad, w_slot_elems / p->tn), x_slot / p->tm) / align * align;
#if CHIPLEAP_DATAFLOW != 2
  while (tk < align && p->tm > 1u) {{
    p->tm = p->tm / 2u ? p->tm / 2u : 1u;
    tk = chipleap_min(chipleap_min(k_pad, w_slot_elems / p->tn), x_slot / p->tm) / align * align;
  }}
#endif
  if (tk < align) return -2;
  p->tk = tk;
  p->x_resident = 0u;
  return 0;
}}

/* Program one job and start it. X is M x k_pad int8 (row-major), W is N x k_pad packed
 * weights of fmt bits (row-major, little-endian), Y is M x N int32. */
static inline void chipleap_start(uintptr_t base, uint32_t x, uint32_t w, uint32_t y,
                                  uint32_t m, uint32_t n, uint32_t fmt,
                                  const chipleap_plan_t *p) {{
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_X_ADDR, x);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_W_ADDR, w);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_Y_ADDR, y);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_M, m);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_K, p->k_pad);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_N, n);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_TM, p->tm);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_TK, p->tk);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_FMT, fmt);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_XRES, p->x_resident);
  CHIPLEAP_WRITE(base, CHIPLEAP_REG_CTRL, 1u);
}}

static inline int chipleap_busy(uintptr_t base) {{
  return (int)(CHIPLEAP_READ(base, CHIPLEAP_REG_STATUS) & 1u);
}}

static inline uint32_t chipleap_cycles(uintptr_t base) {{
  return CHIPLEAP_READ(base, CHIPLEAP_REG_CYCLES);
}}

#endif /* CHIPLEAP_DRIVER_H */
